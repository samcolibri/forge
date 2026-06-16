#!/usr/bin/env python3
"""
loop.py — Universal FORGE loop orchestrator.

Usage:
  python3 loop.py                         # reads ./ARCHITECTURE.yaml
  python3 loop.py --project moreland-sdr  # named project
  python3 loop.py --dry-run               # test without executing workers
  python3 loop.py --once                  # one batch then stop
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation
# ---------------------------------------------------------------------------

try:
    import anthropic as _anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FORGE/%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("forge.loop")

# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------

STATES = [
    "queued",
    "processing",
    "pending_approval",
    "approved",
    "exported",
    "done",
    "failed",
    "escalated",
]

# ---------------------------------------------------------------------------
# Worker risk tier mapping
# Keys are lowercase worker name fragments; first match wins.
# ---------------------------------------------------------------------------

WORKER_RISK_TIERS: dict[str, str] = {
    "reporter":     "low",
    "briefer":      "medium",
    "researcher":   "medium",
    "scout":        "medium",
    "enricher":     "medium",
    "validator":    "medium",
    "storyboarder": "medium",
    "scriptwriter": "high",
    "writer":       "high",
    "critic":       "high",
    "qa_scorer":    "high",
    "scorer":       "high",
    "producer":     "high",
}


def resolve_risk_tier(worker_name: str) -> str:
    """Return the Stormbreaker risk tier for a worker name (case-insensitive fragment match)."""
    name_lower = worker_name.lower()
    for fragment, tier in WORKER_RISK_TIERS.items():
        if fragment in name_lower:
            return tier
    return "medium"  # default


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key    TEXT UNIQUE,
            state       TEXT NOT NULL DEFAULT 'queued',
            data        TEXT,
            qa_score    REAL,
            retries     INTEGER DEFAULT 0,
            worker_log  TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER,
            from_state  TEXT,
            to_state    TEXT,
            worker      TEXT,
            notes       TEXT,
            ts          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS human_gates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_id     TEXT,
            item_id     INTEGER,
            state       TEXT DEFAULT 'pending',
            notes       TEXT,
            created_at  TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS loop_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS stormbreaker_ledger (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_name TEXT NOT NULL,
            task_id     TEXT,
            gate_name   TEXT NOT NULL,
            gate_result TEXT NOT NULL,
            passed      BOOLEAN NOT NULL DEFAULT 1,
            evidence    TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS failure_patterns (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_name         TEXT NOT NULL,
            pattern_description TEXT NOT NULL,
            occurrence_count    INTEGER DEFAULT 1,
            last_seen           TEXT,
            mitigation          TEXT
        );
    """)
    conn.commit()
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO loop_meta (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM loop_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def transition(conn: sqlite3.Connection, item_id: int, to_state: str, worker: str = "", notes: str = "") -> None:
    row = conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
    from_state = row["state"] if row else "?"
    ts = now_iso()
    conn.execute(
        "UPDATE items SET state=?, updated_at=? WHERE id=?", (to_state, ts, item_id)
    )
    conn.execute(
        "INSERT INTO state_log (item_id, from_state, to_state, worker, notes, ts) VALUES (?,?,?,?,?,?)",
        (item_id, from_state, to_state, worker, notes, ts),
    )
    conn.commit()
    log.info(f"[item {item_id}] {from_state} -> {to_state} via {worker or 'loop'}")


def append_worker_log(conn: sqlite3.Connection, item_id: int, worker_name: str, output: str) -> None:
    row = conn.execute("SELECT worker_log FROM items WHERE id=?", (item_id,)).fetchone()
    existing = json.loads(row["worker_log"] or "[]")
    existing.append({"worker": worker_name, "output": output[:2000], "ts": now_iso()})
    conn.execute(
        "UPDATE items SET worker_log=? WHERE id=?", (json.dumps(existing), item_id)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hephaestus Stormbreaker — 8-gate execution protocol
# ---------------------------------------------------------------------------


class StormBreakerGate:
    """
    Hephaestus Stormbreaker execution protocol — 8 gates per work item.

    Risk tiers:
      LOW    (REPORTER):                scope_lock + final_gate
      MEDIUM (SCOUT, ENRICHER, etc.):   + issue_contract + failure_memory
                                          + evidence_loop + outcome_ledger
      HIGH   (WRITER, PRODUCER, etc.):  all 8 gates
    """

    RISK_TIERS: dict[str, list[str]] = {
        "low": [
            "scope_lock",
            "final_gate",
        ],
        "medium": [
            "scope_lock",
            "issue_contract",
            "failure_memory",
            "evidence_loop",
            "outcome_ledger",
            "final_gate",
        ],
        "high": [
            "scope_lock",
            "issue_contract",
            "failure_memory",
            "verifier_first_plan",
            "evidence_loop",
            "review_gate",
            "outcome_ledger",
            "final_gate",
        ],
    }

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        worker_name: str,
        task: dict,
        execute_fn: Callable[[dict], str],
        risk_tier: str = "medium",
    ) -> str:
        """
        Run a worker through the Stormbreaker protocol.

        Returns the worker output string, or raises RuntimeError on
        review_gate block or final_gate failure.
        """
        gates = self.RISK_TIERS.get(risk_tier, self.RISK_TIERS["medium"])
        ledger: dict[str, Any] = {
            "worker": worker_name,
            "task_id": str(task.get("id", "")),
            "gates": {},
            "evidence": [],
        }

        log.info(f"[Stormbreaker] {worker_name} | tier={risk_tier} | gates={gates}")

        # Gate 1: scope_lock
        if "scope_lock" in gates:
            ledger["gates"]["scope_lock"] = self._scope_lock(worker_name, task)

        # Gate 2: issue_contract
        if "issue_contract" in gates:
            ledger["gates"]["issue_contract"] = self._issue_contract(worker_name, task)

        # Gate 3: failure_memory
        if "failure_memory" in gates:
            ledger["gates"]["failure_memory"] = self._check_failure_memory(worker_name, task)

        # Gate 4: verifier_first_plan
        if "verifier_first_plan" in gates:
            ledger["gates"]["verifier_first_plan"] = self._make_verifier_plan(worker_name, task)

        # Gate 5: evidence_loop (actual execution with bounded retries)
        result: str
        if "evidence_loop" in gates:
            result, evidence = self._evidence_loop(execute_fn, task, max_retries=2)
            ledger["evidence"] = evidence
        else:
            result = execute_fn(task)

        # Gate 6: review_gate
        if "review_gate" in gates:
            review = self._review_gate(worker_name, task, result)
            ledger["gates"]["review_gate"] = review
            if review.get("blocked"):
                self._write_ledger(ledger)
                raise RuntimeError(
                    f"Stormbreaker review_gate blocked: {review.get('reason')}"
                )

        # Gate 7: outcome_ledger
        if "outcome_ledger" in gates:
            ledger["gates"]["outcome_ledger"] = self._build_outcome_ledger(
                worker_name, task, result, ledger["evidence"]
            )

        # Gate 8: final_gate
        if "final_gate" in gates:
            final = self._final_gate(worker_name, task, result, ledger)
            ledger["gates"]["final_gate"] = final
            if not final.get("passed"):
                self._write_ledger(ledger)
                raise RuntimeError(
                    f"Stormbreaker final_gate failed: {final.get('blocker')}"
                )

        self._write_ledger(ledger)
        return result

    # ------------------------------------------------------------------
    # Gate implementations
    # ------------------------------------------------------------------

    def _scope_lock(self, worker_name: str, task: dict) -> dict:
        """
        Gate 1 — Restate task ownership, boundaries, and exclusions before
        touching anything. Prevents scope creep from the first moment.
        """
        task_id = str(task.get("id", ""))
        outcome = task.get("outcome") or task.get("source", "")
        result = {
            "gate": "scope_lock",
            "worker": worker_name,
            "task_id": task_id,
            "task_summary": str(task)[:400],
            "ownership": worker_name,
            "boundaries": f"Worker {worker_name} processes task {task_id} only.",
            "exclusions": "Must not modify items owned by other workers.",
            "outcome_restated": str(outcome)[:200],
            "ts": now_iso(),
        }
        log.debug(f"[Stormbreaker/scope_lock] {worker_name}:{task_id} locked.")
        self._persist_gate(worker_name, task_id, "scope_lock", result, True)
        return result

    def _issue_contract(self, worker_name: str, task: dict) -> dict:
        """
        Gate 2 — Extract: what MUST change, what MUST NOT change,
        affected files / fields, edge cases.
        """
        task_id = str(task.get("id", ""))
        result = {
            "gate": "issue_contract",
            "worker": worker_name,
            "task_id": task_id,
            "must_change": f"Produce output for task {task_id} as specified.",
            "must_not_change": "Items in states other than 'queued'.",
            "affected_keys": list(task.keys()),
            "edge_cases": [
                "Empty input data",
                "Upstream worker failure leaving partial output",
                "Timeout during generation",
            ],
            "ts": now_iso(),
        }
        log.debug(f"[Stormbreaker/issue_contract] {worker_name}:{task_id} contracted.")
        self._persist_gate(worker_name, task_id, "issue_contract", result, True)
        return result

    def _check_failure_memory(self, worker_name: str, task: dict) -> dict:
        """
        Gate 3 — Load known failure patterns for this worker type from the
        failure_patterns table and surface any relevant mitigations.
        """
        task_id = str(task.get("id", ""))
        patterns: list[dict] = []
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM failure_patterns WHERE worker_name=? ORDER BY occurrence_count DESC LIMIT 5",
                (worker_name,),
            ).fetchall()
            patterns = [dict(r) for r in rows]
            conn.close()
        except Exception as exc:
            log.debug(f"[Stormbreaker/failure_memory] DB read error (non-fatal): {exc}")

        result = {
            "gate": "failure_memory",
            "worker": worker_name,
            "task_id": task_id,
            "known_patterns": patterns,
            "pattern_count": len(patterns),
            "advisory": (
                f"Found {len(patterns)} historical failure pattern(s) for {worker_name}. "
                "Review mitigations before proceeding."
                if patterns
                else f"No known failure patterns for {worker_name}."
            ),
            "ts": now_iso(),
        }
        log.debug(f"[Stormbreaker/failure_memory] {worker_name}:{task_id} — {len(patterns)} patterns.")
        self._persist_gate(worker_name, task_id, "failure_memory", result, True)
        return result

    def _make_verifier_plan(self, worker_name: str, task: dict) -> dict:
        """
        Gate 4 — Define verification commands / criteria BEFORE execution starts.
        Forces explicit success criteria up-front (HIGH risk only).
        """
        task_id = str(task.get("id", ""))
        result = {
            "gate": "verifier_first_plan",
            "worker": worker_name,
            "task_id": task_id,
            "verification_criteria": [
                "Output is non-empty string",
                "Output does not begin with '[STUB]' or '[DRY-RUN]'",
                "Output does not contain raw Python tracebacks",
                "Output length > 20 characters",
            ],
            "verification_commands": [
                "assert len(output) > 20",
                "assert not output.startswith('[STUB]')",
                "assert 'Traceback' not in output",
            ],
            "ts": now_iso(),
        }
        log.debug(f"[Stormbreaker/verifier_first_plan] {worker_name}:{task_id} plan set.")
        self._persist_gate(worker_name, task_id, "verifier_first_plan", result, True)
        return result

    def _evidence_loop(
        self,
        execute_fn: Callable[[dict], str],
        task: dict,
        max_retries: int = 2,
    ) -> tuple[str, list[dict]]:
        """
        Gate 5 — Bounded execution with evidence logged per attempt.
        Returns (final_output, evidence_list). Never raises.
        """
        evidence: list[dict] = []
        last_output = ""
        for attempt in range(max_retries + 1):
            attempt_ts = now_iso()
            try:
                output = execute_fn(task)
                evidence.append({
                    "attempt": attempt + 1,
                    "ts": attempt_ts,
                    "success": True,
                    "output_preview": output[:300],
                })
                log.debug(f"[Stormbreaker/evidence_loop] attempt {attempt + 1} succeeded.")
                return output, evidence
            except Exception as exc:
                evidence.append({
                    "attempt": attempt + 1,
                    "ts": attempt_ts,
                    "success": False,
                    "error": str(exc)[:300],
                })
                log.warning(
                    f"[Stormbreaker/evidence_loop] attempt {attempt + 1} failed: {exc}"
                )
                last_output = f"[evidence_loop error attempt {attempt + 1}] {str(exc)[:200]}"
        return last_output, evidence

    def _review_gate(self, worker_name: str, task: dict, result: str) -> dict:
        """
        Gate 6 — Check for scope drift, destructive changes, security exposure.
        Returns dict with 'blocked' bool and 'reason' if blocked (HIGH risk only).
        """
        task_id = str(task.get("id", ""))
        findings: list[str] = []
        blocked = False
        reason = ""

        # Heuristic checks on the output string
        danger_phrases = [
            "DROP TABLE", "DELETE FROM", "rm -rf", "os.remove",
            "shutil.rmtree", "subprocess.run([\"rm\"",
            "password", "secret", "api_key", "private_key",
        ]
        for phrase in danger_phrases:
            if phrase.lower() in result.lower():
                findings.append(f"Potentially dangerous pattern detected: '{phrase}'")

        if len(result) > 50_000:
            findings.append(f"Output suspiciously large ({len(result)} chars) — possible runaway generation")

        if findings:
            blocked = True
            reason = "; ".join(findings)

        gate_result = {
            "gate": "review_gate",
            "worker": worker_name,
            "task_id": task_id,
            "blocked": blocked,
            "reason": reason,
            "findings": findings,
            "output_length": len(result),
            "ts": now_iso(),
        }
        level = "warning" if blocked else "debug"
        getattr(log, level)(
            f"[Stormbreaker/review_gate] {worker_name}:{task_id} blocked={blocked}"
        )
        self._persist_gate(worker_name, task_id, "review_gate", gate_result, not blocked)
        return gate_result

    def _build_outcome_ledger(
        self,
        worker_name: str,
        task: dict,
        result: str,
        evidence: list[dict],
    ) -> dict:
        """
        Gate 7 — Document: what worked, what failed, what is unresolved, risk profile.
        """
        task_id = str(task.get("id", ""))
        successful_attempts = [e for e in evidence if e.get("success")]
        failed_attempts = [e for e in evidence if not e.get("success")]

        risk_profile = "low"
        if failed_attempts:
            ratio = len(failed_attempts) / max(len(evidence), 1)
            if ratio >= 0.66:
                risk_profile = "high"
            elif ratio >= 0.33:
                risk_profile = "medium"

        gate_result = {
            "gate": "outcome_ledger",
            "worker": worker_name,
            "task_id": task_id,
            "what_worked": f"{len(successful_attempts)} of {len(evidence)} attempt(s) succeeded.",
            "what_failed": (
                [e.get("error", "") for e in failed_attempts]
                if failed_attempts
                else []
            ),
            "unresolved": (
                []
                if successful_attempts
                else ["All attempts failed — output may be stub/error."]
            ),
            "risk_profile": risk_profile,
            "output_preview": result[:300],
            "ts": now_iso(),
        }
        log.debug(f"[Stormbreaker/outcome_ledger] {worker_name}:{task_id} risk={risk_profile}")
        self._persist_gate(worker_name, task_id, "outcome_ledger", gate_result, True)
        return gate_result

    def _final_gate(
        self,
        worker_name: str,
        task: dict,
        result: str,
        ledger: dict,
    ) -> dict:
        """
        Gate 8 — Confirm all required checks passed, blockers reported,
        risk separated from fact. Returns dict with 'passed' bool and
        'blocker' string if failed.
        """
        task_id = str(task.get("id", ""))
        blockers: list[str] = []

        # Check review_gate did not block (only present for HIGH tier)
        rg = ledger.get("gates", {}).get("review_gate", {})
        if rg.get("blocked"):
            blockers.append(f"review_gate blocked: {rg.get('reason', 'unknown')}")

        # Check outcome_ledger risk profile (only present for MEDIUM+)
        ol = ledger.get("gates", {}).get("outcome_ledger", {})
        if ol.get("risk_profile") == "high":
            blockers.append("outcome_ledger reports HIGH risk — all attempts failed")

        # Check result is meaningful
        if not result or result.strip() == "":
            blockers.append("empty output from worker")

        if result.startswith("[evidence_loop error"):
            blockers.append(f"evidence_loop exhausted all retries: {result[:150]}")

        passed = len(blockers) == 0
        blocker_summary = "; ".join(blockers) if blockers else ""

        gate_result = {
            "gate": "final_gate",
            "worker": worker_name,
            "task_id": task_id,
            "passed": passed,
            "blocker": blocker_summary,
            "checklist": {
                "scope_lock_ran": "scope_lock" in ledger.get("gates", {}),
                "no_review_gate_block": not rg.get("blocked", False),
                "outcome_risk_acceptable": ol.get("risk_profile", "low") != "high",
                "output_non_empty": bool(result and result.strip()),
            },
            "ts": now_iso(),
        }
        log.info(
            f"[Stormbreaker/final_gate] {worker_name}:{task_id} passed={passed}"
            + (f" | blocker: {blocker_summary}" if not passed else "")
        )
        self._persist_gate(worker_name, task_id, "final_gate", gate_result, passed)
        return gate_result

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_gate(
        self,
        worker_name: str,
        task_id: str,
        gate_name: str,
        gate_result: dict,
        passed: bool,
        evidence: list | None = None,
    ) -> None:
        """Write a single gate result to stormbreaker_ledger. Never raises."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                INSERT INTO stormbreaker_ledger
                    (worker_name, task_id, gate_name, gate_result, passed, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker_name,
                    task_id,
                    gate_name,
                    json.dumps(gate_result),
                    int(passed),
                    json.dumps(evidence or []),
                    now_iso(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.debug(f"[Stormbreaker] _persist_gate error (non-fatal): {exc}")

    def _write_ledger(self, ledger: dict) -> None:
        """
        Persist the full ledger snapshot for post-mortem analysis.
        Writes an evidence_summary row capturing attempt counts.
        """
        worker_name = ledger.get("worker", "unknown")
        task_id = ledger.get("task_id", "")
        evidence = ledger.get("evidence", [])

        if evidence:
            self._persist_gate(
                worker_name,
                task_id,
                "evidence_summary",
                {
                    "total_attempts": len(evidence),
                    "successes": sum(1 for e in evidence if e.get("success")),
                },
                passed=any(e.get("success") for e in evidence),
                evidence=evidence,
            )

    def record_failure(
        self,
        worker_name: str,
        pattern_description: str,
        mitigation: str = "",
    ) -> None:
        """
        Public helper: record a new failure pattern (or increment its counter)
        in the failure_patterns table. Call this from escalation handlers so
        Stormbreaker learns over time.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            existing = conn.execute(
                "SELECT id, occurrence_count FROM failure_patterns "
                "WHERE worker_name=? AND pattern_description=?",
                (worker_name, pattern_description),
            ).fetchone()
            ts = now_iso()
            if existing:
                conn.execute(
                    "UPDATE failure_patterns SET occurrence_count=occurrence_count+1, last_seen=? WHERE id=?",
                    (ts, existing[0]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO failure_patterns
                        (worker_name, pattern_description, occurrence_count, last_seen, mitigation)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (worker_name, pattern_description, ts, mitigation),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.debug(f"[Stormbreaker] record_failure error (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# Worker dispatch
# ---------------------------------------------------------------------------


def dispatch_claude_api(worker: dict, input_data: dict, dry_run: bool = False) -> str:
    if dry_run:
        return f"[DRY-RUN] {worker['name']} would process: {str(input_data)[:100]}"

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or not ANTHROPIC_AVAILABLE:
        log.warning(f"[{worker['name']}] ANTHROPIC_API_KEY missing or anthropic not installed — using stub output")
        return f"[STUB] {worker['name']} output for: {str(input_data)[:100]}"

    client = _anthropic.Anthropic(api_key=api_key)
    system_prompt = worker.get("system_prompt", f"You are {worker['name']}. Process the input and produce output.")
    user_msg = f"Input data:\n{json.dumps(input_data, indent=2)}"

    try:
        message = client.messages.create(
            model=worker.get("model", "claude-sonnet-4-6"),
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return message.content[0].text
    except Exception as e:
        log.warning(f"[{worker['name']}] Claude API error: {e} — using stub")
        return f"[STUB/ERROR] {worker['name']}: {str(e)[:200]}"


def dispatch_bash(worker: dict, input_data: dict, dry_run: bool = False) -> str:
    """Dispatch a bash worker. Command is split via shlex for safety — no shell=True."""
    cmd_template = worker.get("command", "echo no-command-configured")
    cmd_str = cmd_template.replace("{{INPUT}}", json.dumps(input_data))

    if dry_run:
        return f"[DRY-RUN] bash: {cmd_str[:100]}"

    try:
        cmd_args = shlex.split(cmd_str)
        result = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            log.warning(f"[{worker['name']}] bash exit code {result.returncode}: {result.stderr[:200]}")
        return output or "[bash: no output]"
    except subprocess.TimeoutExpired:
        return "[bash: timeout after 120s]"
    except Exception as e:
        return f"[bash: error] {str(e)[:200]}"


def dispatch_http(worker: dict, input_data: dict, dry_run: bool = False) -> str:
    endpoint = worker.get("endpoint", "")
    method = worker.get("method", "POST").upper()

    if dry_run:
        return f"[DRY-RUN] http {method} {endpoint}"

    if not HTTPX_AVAILABLE:
        log.warning(f"[{worker['name']}] httpx not installed — using stub")
        return f"[STUB] http {method} {endpoint}"

    try:
        with httpx.Client(timeout=30) as client:
            if method == "POST":
                resp = client.post(endpoint, json=input_data)
            else:
                resp = client.get(endpoint, params=input_data)
            resp.raise_for_status()
            return resp.text[:2000]
    except Exception as e:
        log.warning(f"[{worker['name']}] HTTP error: {e}")
        return f"[http: error] {str(e)[:200]}"


def dispatch_worker(worker: dict, input_data: dict, dry_run: bool = False) -> str:
    dispatch_type = worker.get("dispatch", "claude_api")
    if dispatch_type == "bash":
        return dispatch_bash(worker, input_data, dry_run)
    elif dispatch_type == "http":
        return dispatch_http(worker, input_data, dry_run)
    else:
        return dispatch_claude_api(worker, input_data, dry_run)


# ---------------------------------------------------------------------------
# QA scoring (legacy path — kept for backward-compat; Stormbreaker supersedes)
# ---------------------------------------------------------------------------


def qa_score(output: str, qa_config: dict, dry_run: bool = False) -> float:
    if dry_run:
        return 0.85

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or not ANTHROPIC_AVAILABLE:
        log.warning("[QA] ANTHROPIC_API_KEY missing — defaulting score to 0.8")
        return 0.80

    rubric = qa_config.get("rubric", [])
    rubric_text = "\n".join(
        f"- {r['criterion']} (weight: {r['weight']})" for r in rubric
    )
    scorer_model = qa_config.get("scorer_model", "claude-sonnet-4-6")

    client = _anthropic.Anthropic(api_key=api_key)
    system = textwrap.dedent("""
        You are a QA scorer for an autonomous agent system.
        Given an output and rubric, return a single float between 0.0 and 1.0.
        Return ONLY the float — nothing else.
    """).strip()

    user_msg = f"Rubric:\n{rubric_text}\n\nOutput to score:\n{output[:2000]}"

    try:
        message = client.messages.create(
            model=scorer_model,
            max_tokens=10,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        score_str = message.content[0].text.strip()
        return max(0.0, min(1.0, float(score_str)))
    except Exception as e:
        log.warning(f"[QA] Scoring error: {e} — defaulting to 0.75")
        return 0.75


# ---------------------------------------------------------------------------
# Stop condition check
# ---------------------------------------------------------------------------


def check_stop_conditions(conn: sqlite3.Connection, arch: dict, iteration: int) -> tuple[bool, str]:
    loop_cfg = arch.get("loop", {})
    stop_conds = loop_cfg.get("stop_conditions", {})

    max_iter = 100
    if isinstance(stop_conds, list):
        for cond in stop_conds:
            if isinstance(cond, dict) and "max_iterations" in cond:
                max_iter = int(cond["max_iterations"])
    elif isinstance(stop_conds, dict):
        max_iter = int(stop_conds.get("max_iterations", 100))

    if iteration >= max_iter:
        return True, f"max_iterations ({max_iter}) reached"

    stop_flag = get_meta(conn, "human_stop", "0")
    if stop_flag == "1":
        return True, "human stop flag set"

    total = conn.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
    if total > 0:
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM items WHERE state IN ('failed','escalated')"
        ).fetchone()["c"]
        threshold = 0.3
        if isinstance(stop_conds, dict):
            threshold = float(stop_conds.get("error_threshold", 0.3))
        if (failed / total) > threshold:
            return True, f"error threshold exceeded ({failed}/{total} failed)"

    not_done = conn.execute(
        "SELECT COUNT(*) as c FROM items WHERE state NOT IN ('done','failed','escalated')"
    ).fetchone()["c"]
    if total > 0 and not_done == 0:
        return True, "all items processed — outcome check required"

    return False, ""


# ---------------------------------------------------------------------------
# Hourly reporter trigger
# ---------------------------------------------------------------------------


def maybe_report(last_report_time: float, project_dir: Path, dry_run: bool) -> float:
    now = time.time()
    if now - last_report_time >= 3600:
        log.info("[FORGE] Triggering hourly report...")
        reporter = Path.home() / "projects" / "forge" / "engine" / "reporter.py"
        if reporter.exists() and not dry_run:
            subprocess.Popen(
                [sys.executable, str(reporter)],
                cwd=str(project_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            log.info("[FORGE] Reporter not found or dry-run — skipping email send")
        return now
    return last_report_time


# ---------------------------------------------------------------------------
# Seed queue
# ---------------------------------------------------------------------------


def seed_queue_if_empty(conn: sqlite3.Connection, arch: dict) -> None:
    count = conn.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
    if count == 0:
        log.info("[FORGE] Queue is empty — seeding with placeholder item to start loop")
        ts = now_iso()
        conn.execute(
            "INSERT INTO items (item_key, state, data, created_at, updated_at) VALUES (?,?,?,?,?)",
            (
                "seed_001",
                "queued",
                json.dumps({"source": "seed", "outcome": arch.get("system", {}).get("outcome", "")}),
                ts,
                ts,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# agmsg parallel dispatcher
# ---------------------------------------------------------------------------


class AgmsgDispatcher:
    """
    Parallel worker dispatch via agmsg messaging bus.
    Falls back to sequential direct dispatch if agmsg unavailable.

    When agmsg IS available, worker tasks are fanned out via the bus so
    multiple Claude Code / Codex / AGY sessions can process them concurrently.
    When agmsg is NOT available the existing StormBreakerGate sequential path
    is used transparently — the loop continues to work with no agmsg install.

    Usage:
        dispatcher = AgmsgDispatcher("moreland-sdr", agmsg_available=True)
        results = dispatcher.dispatch_batch("SCOUT", tasks, execute_fn, stormbreaker)
    """

    def __init__(self, project_name: str, agmsg_available: bool) -> None:
        self.team = f"forge-{project_name.lstrip('forge-').lstrip('forge-')}"
        # Normalise: strip any leading "forge-" duplicates
        if not self.team.startswith("forge-"):
            self.team = f"forge-{project_name}"
        self.available = agmsg_available
        self.collector_id = "FORGE_LOOP"

    def dispatch_batch(
        self,
        worker_name: str,
        tasks: list[dict],
        execute_fn: Callable[[dict], str],
        stormbreaker: StormBreakerGate,
    ) -> list[dict]:
        """
        Dispatch a batch of tasks to workers.

        With agmsg:    fans all tasks out in parallel, collects results.
        Without agmsg: runs sequentially using existing StormBreakerGate.

        Returns list of result dicts (each has at minimum 'body' and 'from' keys
        when coming from agmsg; or the raw output string wrapped in a dict when
        coming from the sequential path).
        """
        if self.available:
            return self._dispatch_parallel(worker_name, tasks)
        else:
            return self._dispatch_sequential(worker_name, tasks, execute_fn, stormbreaker)

    def _dispatch_parallel(self, worker_name: str, tasks: list[dict]) -> list[dict]:
        """Send all tasks at once via agmsg, collect results concurrently."""
        from engine.agmsg_bus import dispatch_parallel, collect_parallel
        worker_ids = dispatch_parallel(self.team, tasks, worker_name, self.collector_id)
        responses = collect_parallel(self.team, self.collector_id, worker_ids, timeout=300)
        return [r for r in responses.values() if r is not None]

    def _dispatch_sequential(
        self,
        worker_name: str,
        tasks: list[dict],
        execute_fn: Callable[[dict], str],
        stormbreaker: StormBreakerGate,
    ) -> list[dict]:
        """Original sequential execution path (agmsg not available or disabled)."""
        results = []
        risk = resolve_risk_tier(worker_name)
        for task in tasks:
            try:
                output = stormbreaker.run(worker_name, task, execute_fn, risk)
                results.append({"body": output, "from": worker_name, "timestamp": now_iso()})
            except RuntimeError as e:
                log.warning(f"[FORGE] {worker_name} escalated: {e}")
        return results

    def notify_gate2(self, pending_outputs: list[dict]) -> bool:
        """
        Notify SAM of pending Gate 2 approvals via agmsg.
        No-op (returns False) when agmsg is unavailable.
        """
        if not self.available:
            return False
        from engine.agmsg_bus import gate2_approval_request
        return gate2_approval_request(self.team, pending_outputs)

    def check_sam_approvals(self) -> list[dict]:
        """
        Check if SAM has responded with approvals via agmsg.
        Returns empty list when agmsg is unavailable.
        """
        if not self.available:
            return []
        from engine.agmsg_bus import read_sam_approvals
        return read_sam_approvals(self.team)



async def run_loop(arch: dict, conn: sqlite3.Connection, dry_run: bool, once: bool) -> None:
    workers: list[dict] = arch.get("workers", [])
    qa_cfg: dict = arch.get("qa", {})
    qa_threshold: float = float(qa_cfg.get("threshold", 0.75))
    max_retries: int = int(qa_cfg.get("max_retries", 2))
    batch_size: int = int(arch.get("loop", {}).get("batch_size", 10))
    human_gates_cfg: list[dict] = arch.get("human_gates", [])
    gate_states = {
        g["trigger_state"]: g
        for g in human_gates_cfg
        if isinstance(g, dict) and "trigger_state" in g
    }

    db_path_str = arch.get("loop", {}).get("state_store", "./forge_state.db")
    stormbreaker = StormBreakerGate(db_path=db_path_str)

    # ------------------------------------------------------------------
    # agmsg dispatcher — parallel fan-out when agmsg is installed
    # ------------------------------------------------------------------
    try:
        from engine.agmsg_bus import is_available as _agmsg_available
        _agmsg_ok = _agmsg_available()
    except ImportError:
        _agmsg_ok = False

    _project_name = arch.get("system", {}).get("name", "unknown")
    dispatcher = AgmsgDispatcher(_project_name, agmsg_available=_agmsg_ok)
    if _agmsg_ok:
        log.info(f"[FORGE] agmsg bus ONLINE — team: {dispatcher.team}")
    else:
        log.info("[FORGE] agmsg bus OFFLINE — sequential dispatch mode")

    seed_queue_if_empty(conn, arch)

    iteration = 0
    last_report_time = time.time()

    log.info(
        f"[FORGE] Loop starting. Workers: {[w['name'] for w in workers]}. "
        f"Batch: {batch_size}. QA threshold: {qa_threshold}. "
        f"Stormbreaker: ENABLED"
    )
    set_meta(conn, "loop_start", now_iso())
    set_meta(conn, "human_stop", "0")

    while True:
        iteration += 1
        log.info(f"[FORGE] === Batch {iteration} ===")

        should_stop, reason = check_stop_conditions(conn, arch, iteration)
        if should_stop:
            log.info(f"[FORGE] Stop condition met: {reason}")
            set_meta(conn, "stop_reason", reason)
            set_meta(conn, "loop_end", now_iso())
            break

        items = conn.execute(
            "SELECT * FROM items WHERE state='queued' LIMIT ?", (batch_size,)
        ).fetchall()

        if not items:
            log.info("[FORGE] No queued items this batch — waiting 30s")
            if once:
                break
            await asyncio.sleep(30)
            last_report_time = maybe_report(last_report_time, Path("."), dry_run)
            continue

        for item in items:
            item_id = item["id"]
            item_data = json.loads(item["data"] or "{}")
            current_output = item_data

            transition(conn, item_id, "processing")

            pipeline_failed = False
            for worker in workers:
                worker_name = worker.get("name", "UNKNOWN")
                risk_tier = resolve_risk_tier(worker_name)
                log.info(
                    f"[item {item_id}] Running {worker_name} "
                    f"(Stormbreaker tier={risk_tier})..."
                )

                # Build Stormbreaker task dict
                sb_task: dict = {
                    "id": str(item_id),
                    "item_data": current_output,
                    "worker_name": worker_name,
                }

                # Capture loop variables for the closure
                def make_execute_fn(
                    w: dict, inp: dict, dr: bool
                ) -> Callable[[dict], str]:
                    def execute_fn(_task: dict) -> str:
                        return dispatch_worker(w, inp, dr)
                    return execute_fn

                execute_fn = make_execute_fn(worker, current_output, dry_run)

                try:
                    output_text = stormbreaker.run(
                        worker_name=worker_name,
                        task=sb_task,
                        execute_fn=execute_fn,
                        risk_tier=risk_tier,
                    )
                except RuntimeError as sb_err:
                    log.warning(
                        f"[item {item_id}] Stormbreaker blocked {worker_name}: {sb_err}"
                    )
                    stormbreaker.record_failure(
                        worker_name=worker_name,
                        pattern_description=str(sb_err)[:400],
                        mitigation="Investigate gate that blocked and fix root cause.",
                    )
                    transition(
                        conn, item_id, "escalated", worker_name,
                        f"Stormbreaker: {str(sb_err)[:200]}",
                    )
                    pipeline_failed = True
                    break

                append_worker_log(conn, item_id, worker_name, output_text)

                # Legacy QA scoring path — still runs for writer-role workers so
                # existing ARCHITECTURE.yaml rubrics are honoured. Stormbreaker's
                # evidence_loop provides the primary retry guard; this adds the
                # rubric-based score on top.
                role = worker.get("role", "").lower()
                is_writer = any(
                    kw in role
                    for kw in ["writer", "drafter", "generator", "author", "composer"]
                )
                is_qa_worker = (
                    "qa" in worker_name.lower() or "scorer" in worker_name.lower()
                )

                if is_writer and not is_qa_worker and qa_cfg:
                    score = qa_score(output_text, qa_cfg, dry_run)
                    log.info(
                        f"[item {item_id}] QA score: {score:.2f} (threshold: {qa_threshold})"
                    )
                    conn.execute(
                        "UPDATE items SET qa_score=? WHERE id=?", (score, item_id)
                    )
                    conn.commit()

                    if score < qa_threshold:
                        retries = item["retries"]
                        if retries < max_retries:
                            conn.execute(
                                "UPDATE items SET retries=retries+1 WHERE id=?", (item_id,)
                            )
                            conn.commit()
                            log.info(
                                f"[item {item_id}] QA fail — retry {retries+1}/{max_retries}"
                            )
                            feedback_input = {
                                **current_output,
                                "_qa_feedback": f"Previous score: {score:.2f}. Improve quality.",
                            }
                            retry_task = {**sb_task, "item_data": feedback_input}
                            output_text = stormbreaker.run(
                                worker_name=f"{worker_name}/retry",
                                task=retry_task,
                                execute_fn=make_execute_fn(worker, feedback_input, dry_run),
                                risk_tier=risk_tier,
                            )
                            append_worker_log(
                                conn, item_id, f"{worker_name}/retry", output_text
                            )
                        else:
                            log.warning(
                                f"[item {item_id}] QA fail after {max_retries} retries — escalating"
                            )
                            stormbreaker.record_failure(
                                worker_name=worker_name,
                                pattern_description=(
                                    f"QA score {score:.2f} consistently below "
                                    f"threshold {qa_threshold}"
                                ),
                                mitigation="Review system_prompt and rubric alignment.",
                            )
                            transition(
                                conn, item_id, "escalated", worker_name,
                                f"QA score {score:.2f} below threshold {qa_threshold}",
                            )
                            pipeline_failed = True
                            break

                current_output = {
                    "_prev_worker": worker_name,
                    "_output": output_text,
                    **current_output,
                }

                if worker_name in gate_states:
                    gate = gate_states[worker_name]
                    log.info(
                        f"[item {item_id}] Human gate: {gate.get('id', 'gate')} — "
                        f"{gate.get('description', '')}"
                    )
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO human_gates (gate_id, item_id, state, created_at) VALUES (?,?,?,?)",
                        (gate.get("id", "gate"), item_id, "pending", ts),
                    )
                    conn.commit()
                    transition(
                        conn, item_id, "pending_approval", worker_name,
                        f"gate: {gate.get('id')}",
                    )
                    # Notify SAM via agmsg for async Gate 2 approval
                    pending_item = {
                        "id": str(item_id),
                        "account": item_data.get("account", item_data.get("source", f"item-{item_id}")),
                        "type": gate.get("id", "output"),
                        "qa_score": conn.execute(
                            "SELECT qa_score FROM items WHERE id=?", (item_id,)
                        ).fetchone()["qa_score"] or 0.0,
                    }
                    dispatcher.notify_gate2([pending_item])
                    pipeline_failed = True
                    break

            if not pipeline_failed:
                transition(conn, item_id, "exported", "loop")
                transition(conn, item_id, "done", "loop", "pipeline complete")

        # ----------------------------------------------------------------
        # Check SAM inbox for Gate 2 approval responses (non-blocking)
        # ----------------------------------------------------------------
        sam_approvals = dispatcher.check_sam_approvals()
        for approval in sam_approvals:
            action = approval.get("action", "")
            ids = approval.get("item_ids", [])
            feedback = approval.get("feedback", "")
            if action == "approved":
                log.info(f"[FORGE] SAM approved items: {ids}")
                for item_id_str in ids:
                    if item_id_str == "all":
                        # Approve all pending_approval items
                        pending_rows = conn.execute(
                            "SELECT id FROM items WHERE state='pending_approval'"
                        ).fetchall()
                        for row in pending_rows:
                            transition(conn, row["id"], "approved", "SAM", "Gate 2 approved")
                    else:
                        try:
                            approved_id = int(item_id_str)
                            transition(conn, approved_id, "approved", "SAM", "Gate 2 approved")
                        except (ValueError, TypeError):
                            log.warning(f"[FORGE] SAM approval: invalid item id {item_id_str!r}")
            elif action == "rejected":
                log.warning(f"[FORGE] SAM rejected items {ids}: {feedback}")
                for item_id_str in ids:
                    try:
                        rejected_id = int(item_id_str)
                        transition(
                            conn, rejected_id, "failed", "SAM",
                            f"Gate 2 rejected: {feedback[:200]}",
                        )
                    except (ValueError, TypeError):
                        log.warning(f"[FORGE] SAM rejection: invalid item id {item_id_str!r}")

        if once:
            log.info("[FORGE] --once flag set — exiting after first batch")
            break

        last_report_time = maybe_report(last_report_time, Path("."), dry_run)
        await asyncio.sleep(5)

    stats = conn.execute("""
        SELECT state, COUNT(*) as cnt FROM items GROUP BY state
    """).fetchall()
    log.info("[FORGE] Final state summary:")
    for row in stats:
        log.info(f"  {row['state']}: {row['cnt']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal FORGE loop orchestrator.")
    parser.add_argument("--project", help="Named project (for logging only)")
    parser.add_argument("--arch", default="./ARCHITECTURE.yaml", help="Path to ARCHITECTURE.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Test without executing workers")
    parser.add_argument("--once", action="store_true", help="One batch then stop")
    args = parser.parse_args()

    arch_path = Path(args.arch)
    if not arch_path.exists():
        log.error(f"ARCHITECTURE.yaml not found at {arch_path}. Run architect.py first.")
        sys.exit(1)

    try:
        arch = yaml.safe_load(arch_path.read_text())
    except yaml.YAMLError as e:
        log.error(f"Invalid ARCHITECTURE.yaml: {e}")
        sys.exit(1)

    db_path_str = arch.get("loop", {}).get("state_store", "./forge_state.db")
    db_path = Path(db_path_str)
    conn = init_db(db_path)

    project_name = args.project or arch.get("system", {}).get("name", "unknown")
    log.info(f"[FORGE] Starting loop for project: {project_name}")
    if args.dry_run:
        log.info("[FORGE] DRY-RUN mode — no real API calls or side effects")

    def handle_signal(signum, frame):
        log.info(f"[FORGE] Signal {signum} received — setting human_stop flag")
        set_meta(conn, "human_stop", "1")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(run_loop(arch, conn, args.dry_run, args.once))
    conn.close()


if __name__ == "__main__":
    main()

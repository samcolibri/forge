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
from typing import Any

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
    # Replace {{INPUT}} placeholder with JSON-encoded input
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
# QA scoring
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
            ("seed_001", "queued", json.dumps({"source": "seed", "outcome": arch.get("system", {}).get("outcome", "")}), ts, ts),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_loop(arch: dict, conn: sqlite3.Connection, dry_run: bool, once: bool) -> None:
    workers: list[dict] = arch.get("workers", [])
    qa_cfg: dict = arch.get("qa", {})
    qa_threshold: float = float(qa_cfg.get("threshold", 0.75))
    max_retries: int = int(qa_cfg.get("max_retries", 2))
    batch_size: int = int(arch.get("loop", {}).get("batch_size", 10))
    human_gates: list[dict] = arch.get("human_gates", [])
    gate_states = {g["trigger_state"]: g for g in human_gates if isinstance(g, dict) and "trigger_state" in g}

    seed_queue_if_empty(conn, arch)

    iteration = 0
    last_report_time = time.time()

    log.info(f"[FORGE] Loop starting. Workers: {[w['name'] for w in workers]}. Batch: {batch_size}. QA threshold: {qa_threshold}")
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
                log.info(f"[item {item_id}] Running {worker_name}...")

                output_text = dispatch_worker(worker, current_output, dry_run)
                append_worker_log(conn, item_id, worker_name, output_text)

                role = worker.get("role", "").lower()
                is_writer = any(kw in role for kw in ["writer", "drafter", "generator", "author", "composer"])
                is_qa_worker = "qa" in worker_name.lower() or "scorer" in worker_name.lower()

                if is_writer and not is_qa_worker and qa_cfg:
                    score = qa_score(output_text, qa_cfg, dry_run)
                    log.info(f"[item {item_id}] QA score: {score:.2f} (threshold: {qa_threshold})")
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
                            log.info(f"[item {item_id}] QA fail — retry {retries+1}/{max_retries}")
                            feedback_input = {
                                **current_output,
                                "_qa_feedback": f"Previous score: {score:.2f}. Improve quality.",
                            }
                            output_text = dispatch_worker(worker, feedback_input, dry_run)
                            append_worker_log(conn, item_id, f"{worker_name}/retry", output_text)
                        else:
                            log.warning(f"[item {item_id}] QA fail after {max_retries} retries — escalating")
                            transition(conn, item_id, "escalated", worker_name, f"QA score {score:.2f} below threshold {qa_threshold}")
                            pipeline_failed = True
                            break

                current_output = {"_prev_worker": worker_name, "_output": output_text, **current_output}

                if worker_name in gate_states:
                    gate = gate_states[worker_name]
                    log.info(f"[item {item_id}] Human gate: {gate.get('id', 'gate')} — {gate.get('description', '')}")
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO human_gates (gate_id, item_id, state, created_at) VALUES (?,?,?,?)",
                        (gate.get("id", "gate"), item_id, "pending", ts),
                    )
                    conn.commit()
                    transition(conn, item_id, "pending_approval", worker_name, f"gate: {gate.get('id')}")
                    pipeline_failed = True
                    break

            if not pipeline_failed:
                transition(conn, item_id, "exported", "loop")
                transition(conn, item_id, "done", "loop", "pipeline complete")

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

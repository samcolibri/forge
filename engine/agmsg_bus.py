#!/usr/bin/env python3
"""
agmsg_bus.py — Python interface to agmsg cross-agent messaging bus.

FORGE uses agmsg to dispatch work to parallel worker agent sessions
(Claude Code, Codex, AGY) and collect results. Sam joins as "SAM"
and receives Gate 2 approval requests directly in his Claude Code session.

Team naming: forge-{project_name}
Worker IDs for parallel dispatch: SCOUT_1, SCOUT_2, SCOUT_3 etc.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("forge.agmsg_bus")

# ---------------------------------------------------------------------------
# Script paths
# ---------------------------------------------------------------------------

AGMSG_SCRIPTS = os.path.expanduser("~/.agents/skills/agmsg/scripts")
AGMSG_DB = os.path.expanduser("~/.agents/skills/agmsg/db/messages.db")

# Subprocess timeout for individual script calls (seconds)
_CALL_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(script: str, args: list[str], input_text: str | None = None) -> tuple[bool, str]:
    """
    Run an agmsg shell script with the given positional args.

    Returns (success: bool, stdout: str).
    Never raises — all exceptions are caught and logged.
    """
    script_path = Path(AGMSG_SCRIPTS) / script
    if not script_path.exists():
        log.warning(f"[agmsg_bus] Script not found: {script_path}")
        return False, ""

    cmd = [str(script_path)] + [str(a) for a in args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CALL_TIMEOUT,
            input=input_text,
        )
        if result.returncode != 0:
            log.warning(
                f"[agmsg_bus] {script} exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
            return False, result.stdout.strip()
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning(f"[agmsg_bus] {script} timed out after {_CALL_TIMEOUT}s")
        return False, ""
    except Exception as exc:
        log.warning(f"[agmsg_bus] {script} exception: {exc}")
        return False, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_available() -> bool:
    """Check if agmsg is installed and usable."""
    scripts_dir = Path(AGMSG_SCRIPTS)
    required = ["send.sh", "inbox.sh", "join.sh", "team.sh"]
    if not scripts_dir.is_dir():
        return False
    for script in required:
        if not (scripts_dir / script).exists():
            return False
    return True


def team_name(project_name: str) -> str:
    """
    Normalize a project name to a FORGE team slug.

    forge-moreland-sdr -> forge-moreland-sdr (already canonical)
    moreland-sdr       -> forge-moreland-sdr
    """
    name = project_name.strip().lower()
    if not name.startswith("forge-"):
        name = f"forge-{name}"
    return name


def send(team: str, from_agent: str, to_agent: str, message: str) -> bool:
    """
    Send a message via agmsg.

    Returns True on success, False on failure. Never raises.
    """
    ok, _ = _run("send.sh", [team, from_agent, to_agent, message])
    if ok:
        log.debug(f"[agmsg_bus] sent {from_agent} -> {to_agent} @ {team}")
    return ok


def inbox(team: str, agent_id: str) -> list[dict]:
    """
    Get unread messages for agent_id.

    Returns list of dicts with keys: from, body, timestamp.
    Parses the inbox.sh output format (unit-separator delimited records).
    """
    ok, output = _run("inbox.sh", [team, agent_id])
    if not ok or not output:
        return []
    if output in ("No new messages.", "No messages (DB not initialized)"):
        return []

    messages: list[dict] = []
    # inbox.sh output:
    #   N new message(s):
    #   (blank)
    #     [<ts>] <from>: <body>
    #   ...
    #   (blank)
    # Each body may have literal \n for embedded newlines (agmsg escapes them).
    for line in output.splitlines():
        line = line.strip()
        # Match lines that look like: [2026-06-15T12:34:56Z] AGENT_NAME: body text
        match = re.match(r"^\[([^\]]+)\]\s+([^:]+):\s+(.*)$", line)
        if match:
            ts, from_agent, body = match.group(1), match.group(2).strip(), match.group(3)
            # Restore escaped newlines
            body = body.replace("\\n", "\n").replace("\\t", "\t")
            messages.append({"from": from_agent, "body": body, "timestamp": ts})

    return messages


def send_json(team: str, from_agent: str, to_agent: str, data: dict) -> bool:
    """Convenience wrapper: JSON-serialize data and send as message body."""
    try:
        body = json.dumps(data, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        log.warning(f"[agmsg_bus] send_json serialization error: {exc}")
        return False
    return send(team, from_agent, to_agent, body)


def inbox_json(team: str, agent_id: str) -> list[dict]:
    """
    Retrieve unread messages and parse bodies as JSON where possible.

    Each returned dict has: from, body (original string), timestamp, data (parsed
    dict if body was valid JSON, otherwise None).
    """
    messages = inbox(team, agent_id)
    for msg in messages:
        try:
            msg["data"] = json.loads(msg["body"])
        except (json.JSONDecodeError, TypeError):
            msg["data"] = None
    return messages


def wait_for_response(
    team: str,
    agent_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> dict | None:
    """
    Poll inbox until one message arrives or timeout expires.

    Returns the first message dict or None on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = inbox(team, agent_id)
        if msgs:
            return msgs[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    log.warning(
        f"[agmsg_bus] wait_for_response timed out after {timeout}s "
        f"(team={team}, agent={agent_id})"
    )
    return None


def wait_for_responses(
    team: str,
    agent_id: str,
    count: int,
    timeout: int = 300,
    poll_interval: float = 2.0,
) -> list[dict]:
    """
    Poll inbox until `count` messages arrive or timeout expires.

    Returns all collected messages (may be fewer than count on timeout).
    """
    collected: list[dict] = []
    deadline = time.monotonic() + timeout
    while len(collected) < count and time.monotonic() < deadline:
        msgs = inbox(team, agent_id)
        collected.extend(msgs)
        if len(collected) >= count:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    if len(collected) < count:
        log.warning(
            f"[agmsg_bus] wait_for_responses: got {len(collected)}/{count} "
            f"after {timeout}s (team={team}, agent={agent_id})"
        )
    return collected[:count]  # cap at requested count in case of bursts


def broadcast(team: str, from_agent: str, recipients: list[str], message: str) -> int:
    """
    Send the same message to multiple agents.

    Returns the count of successful sends.
    """
    success_count = 0
    for recipient in recipients:
        if send(team, from_agent, recipient, message):
            success_count += 1
    return success_count


def dispatch_parallel(
    team: str,
    tasks: list[dict],
    worker_base: str,
    from_agent: str = "FORGE_LOOP",
) -> list[str]:
    """
    Send N tasks to N uniquely-named workers (WORKER_BASE_1, WORKER_BASE_2, ...).

    Each task is JSON-serialized and sent as the message body so the receiving
    worker agent can parse it on arrival.

    Returns the list of worker IDs that were successfully dispatched to.

    Example:
        worker_ids = dispatch_parallel("forge-moreland", accounts, "SCOUT")
        # -> ["SCOUT_1", "SCOUT_2", "SCOUT_3"]
        results = collect_parallel(team, "FORGE_LOOP", worker_ids)
    """
    dispatched: list[str] = []
    for i, task in enumerate(tasks, start=1):
        worker_id = f"{worker_base}_{i}"
        ok = send_json(team, from_agent, worker_id, task)
        if ok:
            dispatched.append(worker_id)
            log.debug(
                f"[agmsg_bus] dispatched task {i}/{len(tasks)} to {worker_id}"
            )
        else:
            log.warning(
                f"[agmsg_bus] dispatch_parallel: failed to send task {i} to {worker_id}"
            )
    return dispatched


def collect_parallel(
    team: str,
    collector_id: str,
    worker_ids: list[str],
    timeout: int = 300,
) -> dict[str, dict | None]:
    """
    Collect responses from parallel workers.

    Polls the collector's inbox and routes messages by their `from` field.
    Returns a dict mapping worker_id -> response message dict.
    Workers that did not respond within the timeout have value None.
    """
    if not worker_ids:
        return {}

    remaining = set(worker_ids)
    results: dict[str, dict | None] = {wid: None for wid in worker_ids}
    deadline = time.monotonic() + timeout
    poll_interval = 2.0

    while remaining and time.monotonic() < deadline:
        msgs = inbox(team, collector_id)
        for msg in msgs:
            sender = msg.get("from", "")
            if sender in remaining:
                results[sender] = msg
                remaining.discard(sender)
                log.debug(
                    f"[agmsg_bus] collect_parallel: received from {sender} "
                    f"({len(worker_ids) - len(remaining)}/{len(worker_ids)})"
                )

        if remaining:
            elapsed = time.monotonic()
            remaining_secs = deadline - elapsed
            if remaining_secs <= 0:
                break
            time.sleep(min(poll_interval, remaining_secs))

    if remaining:
        log.warning(
            f"[agmsg_bus] collect_parallel: timed out waiting for "
            f"{sorted(remaining)} (timeout={timeout}s)"
        )
    return results


def join_team(
    team: str,
    agent_id: str,
    agent_type: str = "claude-code",
    project_dir: str = ".",
) -> bool:
    """
    Register an agent in a team via agmsg join.sh.

    agent_type must be one of: claude-code, codex, gemini, antigravity, copilot.
    """
    project_path = str(Path(project_dir).resolve())
    ok, out = _run("join.sh", [team, agent_id, agent_type, project_path])
    if ok:
        log.info(f"[agmsg_bus] joined team {team} as {agent_id} ({agent_type})")
    return ok


def spawn_worker(
    team: str,
    worker_name: str,
    agent_type: str = "claude-code",
    project_dir: str = ".",
) -> bool:
    """
    Spawn a new agent session as a team member via agmsg spawn.sh.

    spawn.sh signature: spawn.sh <agent-type> <name> [--team <team>] [--project <path>]
    """
    project_path = str(Path(project_dir).resolve())
    ok, out = _run(
        "spawn.sh",
        [agent_type, worker_name, "--team", team, "--project", project_path, "--no-wait"],
    )
    if ok:
        log.info(f"[agmsg_bus] spawned {worker_name} ({agent_type}) in {team}")
    return ok


def despawn_worker(team: str, worker_name: str, force: bool = False) -> bool:
    """
    Tear down a spawned worker via agmsg despawn.sh.

    despawn.sh signature: despawn.sh <team> <from> <name> [--force]
    Uses FORGE_LOOP as the sender (the loop is always the leader).
    """
    args = [team, "FORGE_LOOP", worker_name]
    if force:
        args.append("--force")
    ok, _ = _run("despawn.sh", args)
    if ok:
        log.info(f"[agmsg_bus] despawned {worker_name} from {team}")
    return ok


def list_team(team: str) -> list[dict]:
    """
    List current team members.

    Returns list of dicts with keys: name, type, project.
    Parses the text output of team.sh.
    """
    ok, output = _run("team.sh", [team])
    if not ok or not output:
        return []

    members: list[dict] = []
    # team.sh output lines look like:
    #   AGENT_NAME (claude-code) -- /path/to/project
    #   AGENT_NAME (claude-code) -- /path/to/project (+2 more)
    for line in output.splitlines():
        line = line.strip()
        match = re.match(r"^(\S+)\s+\(([^)]+)\)\s+[--]+\s+(.+?)(\s+\(\+\d+ more\))?$", line)
        if match:
            members.append(
                {
                    "name": match.group(1),
                    "type": match.group(2),
                    "project": match.group(3).strip(),
                }
            )
    return members


def notify_sam(team: str, message: str, items: list[dict] | None = None) -> bool:
    """
    Send a notification to SAM via agmsg.

    If items are provided they are appended as a numbered list to the message body.
    """
    if items:
        item_lines = "\n".join(
            f"{i}. {_format_item(item)}" for i, item in enumerate(items, 1)
        )
        full_message = f"{message}\n\n{item_lines}"
    else:
        full_message = message

    return send(team, "FORGE_LOOP", "SAM", full_message)


def gate2_approval_request(team: str, pending_outputs: list[dict]) -> bool:
    """
    Full Gate 2 agmsg flow: format a SAM-readable approval request and send it.

    Message format:
        FORGE Gate 2: N outputs pending your approval.

        1. [account] sequence_draft -- QA: 9.1/10
        ...

        Reply: approved [ids] | reject [id] [feedback]
    """
    if not pending_outputs:
        log.debug("[agmsg_bus] gate2_approval_request: no pending outputs to send")
        return False

    count = len(pending_outputs)
    header = f"FORGE Gate 2: {count} output{'s' if count != 1 else ''} pending your approval."

    lines: list[str] = []
    for i, item in enumerate(pending_outputs, 1):
        lines.append(f"{i}. {_format_item(item)}")

    body = "\n".join(lines)
    footer = "Reply: approved [ids] | reject [id] [feedback]"
    full_message = f"{header}\n\n{body}\n\n{footer}"

    ok = send(team, "FORGE_LOOP", "SAM", full_message)
    if ok:
        log.info(
            f"[agmsg_bus] Gate 2 approval request sent to SAM for {count} item(s)"
        )
    return ok


def read_sam_approvals(team: str) -> list[dict]:
    """
    Check SAM's responses in the FORGE_LOOP inbox.

    Parses two reply patterns:
        approved [ids]               -> action=approved, item_ids=[...], feedback=""
        reject [id] [feedback text]  -> action=rejected, item_ids=[id], feedback="..."

    Returns list of parsed action dicts. Unrecognised messages are ignored.
    """
    messages = inbox(team, "FORGE_LOOP")
    parsed: list[dict] = []

    for msg in messages:
        if msg.get("from", "").upper() != "SAM":
            continue
        body = msg.get("body", "").strip()
        action = _parse_sam_reply(body)
        if action:
            parsed.append(action)
        else:
            log.debug(f"[agmsg_bus] read_sam_approvals: unrecognised reply: {body[:80]}")

    return parsed


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _format_item(item: dict) -> str:
    """Format a pending output dict as a short human-readable line."""
    parts: list[str] = []
    if "account" in item:
        parts.append(f"[{item['account']}]")
    if "type" in item:
        parts.append(item["type"])
    elif "name" in item:
        parts.append(item["name"])
    if "qa_score" in item:
        score = item["qa_score"]
        if isinstance(score, float):
            parts.append(f"QA: {score * 10:.1f}/10")
        else:
            parts.append(f"QA: {score}")
    if "id" in item:
        parts.append(f"(id={item['id']})")
    return " -- ".join(parts) if parts else str(item)[:80]


def _parse_sam_reply(body: str) -> dict | None:
    """
    Parse a SAM reply body into a structured action dict.

    Handles:
        approved 1 2 3
        approved all
        reject 2 The hook is wrong, fix the subject line
    """
    body_lower = body.lower().strip()

    # --- approved [ids | all] ---
    approved_match = re.match(
        r"^approved\s+(.+)$", body_lower, re.IGNORECASE
    )
    if approved_match:
        id_part = approved_match.group(1).strip()
        if id_part == "all":
            item_ids: list[str] = ["all"]
        else:
            item_ids = [x.strip() for x in re.split(r"[\s,]+", id_part) if x.strip()]
        return {"action": "approved", "item_ids": item_ids, "feedback": ""}

    # --- reject [id] [feedback] ---
    reject_match = re.match(
        r"^reject\s+(\S+)(?:\s+(.+))?$", body, re.IGNORECASE
    )
    if reject_match:
        item_id = reject_match.group(1).strip()
        feedback = (reject_match.group(2) or "").strip()
        return {"action": "rejected", "item_ids": [item_id], "feedback": feedback}

    return None


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------


def _run_self_tests(team: str) -> None:
    """Basic smoke-tests for local development."""
    import sys

    print(f"\nagmsg_bus self-test  (team={team})")
    print("=" * 50)

    # 1. Availability
    avail = is_available()
    print(f"[1] is_available()        -> {avail}")
    if not avail:
        print("    agmsg not installed -- remaining tests skipped.")
        sys.exit(1)

    # 2. team_name normalisation
    assert team_name("moreland-sdr") == "forge-moreland-sdr", "team_name failed"
    assert team_name("forge-moreland-sdr") == "forge-moreland-sdr", "team_name idempotent failed"
    print("[2] team_name()           -> OK")

    # 3. join_team (join two agents)
    project = str(Path.cwd())
    ok_loop = join_team(team, "FORGE_LOOP", "claude-code", project)
    ok_sam  = join_team(team, "SAM",        "claude-code", project)
    print(f"[3] join_team()           -> FORGE_LOOP={ok_loop}, SAM={ok_sam}")

    # 4. send + inbox
    ok_send = send(team, "FORGE_LOOP", "SAM", "hello from self-test")
    print(f"[4] send()                -> {ok_send}")

    msgs = inbox(team, "SAM")
    print(f"[5] inbox()               -> {len(msgs)} message(s)")

    # 5. send_json / inbox_json
    payload = {"action": "test", "value": 42}
    ok_json = send_json(team, "FORGE_LOOP", "SAM", payload)
    jmsgs   = inbox_json(team, "SAM")
    parsed_ok = any(m.get("data", {}) == payload for m in jmsgs)
    print(f"[6] send_json/inbox_json  -> send={ok_json}, parsed={parsed_ok}")

    # 6. dispatch_parallel -> collect_parallel (loopback: FORGE_LOOP sends to itself)
    tasks = [{"id": i, "work": f"task-{i}"} for i in range(3)]
    dispatched = dispatch_parallel(team, tasks, "WORKER", from_agent="FORGE_LOOP")
    print(f"[7] dispatch_parallel()   -> dispatched to {dispatched}")

    # 7. gate2_approval_request + _parse_sam_reply
    pending = [
        {"account": "First National", "type": "sequence_draft", "qa_score": 0.91, "id": "1"},
        {"account": "Lakeside Bank",  "type": "sequence_draft", "qa_score": 0.87, "id": "2"},
    ]
    ok_gate2 = gate2_approval_request(team, pending)
    print(f"[8] gate2_approval_request() -> {ok_gate2}")

    # 8. _parse_sam_reply
    r1 = _parse_sam_reply("approved 1 2")
    r2 = _parse_sam_reply("reject 2 Subject line is too generic")
    assert r1 == {"action": "approved", "item_ids": ["1", "2"], "feedback": ""}, f"parse failed: {r1}"
    assert r2 == {"action": "rejected", "item_ids": ["2"], "feedback": "Subject line is too generic"}, f"parse failed: {r2}"
    print("[9] _parse_sam_reply()    -> OK")

    # 9. list_team
    members = list_team(team)
    print(f"[10] list_team()          -> {[m['name'] for m in members]}")

    print("\nAll tests passed.")


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [agmsg_bus/%(levelname)s] %(message)s",
    )

    if len(sys.argv) >= 3 and sys.argv[1] == "test":
        _run_self_tests(sys.argv[2])
    else:
        print("Usage: python3 agmsg_bus.py test <team-name>")
        print("Example: python3 agmsg_bus.py test forge-test")
        sys.exit(1)

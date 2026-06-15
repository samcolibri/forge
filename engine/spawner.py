#!/usr/bin/env python3
"""
spawner.py — Start the FORGE loop as a background daemon.

Usage:
  python3 spawner.py                    # spawn loop.py for current dir
  python3 spawner.py --stop             # stop running loop for current dir
  python3 spawner.py --status           # show loop status
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


FORGE_ROOT = Path.home() / "projects" / "forge"
ACTIVE_LOOPS_FILE = FORGE_ROOT / "state" / "active_loops.json"
DEFAULT_RECIPIENT = "sam.chaudhary@alliedschools.com"


# ---------------------------------------------------------------------------
# Active loops registry
# ---------------------------------------------------------------------------


def load_active_loops() -> dict:
    if ACTIVE_LOOPS_FILE.exists():
        try:
            return json.loads(ACTIVE_LOOPS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_active_loops(loops: dict) -> None:
    ACTIVE_LOOPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_LOOPS_FILE.write_text(json.dumps(loops, indent=2))


def get_project_key(project_dir: Path) -> str:
    return str(project_dir.resolve())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PID management
# ---------------------------------------------------------------------------


def read_pid(pid_file: Path) -> Optional[int]:
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def get_last_activity(db_path: Path) -> str:
    if not db_path.exists():
        return "no state DB"
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT updated_at FROM items ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else "no items yet"
    except sqlite3.Error:
        return "DB read error"


def get_loop_stats(db_path: Path) -> dict:
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM items GROUP BY state"
        ).fetchall()
        conn.close()
        return {row["state"]: row["cnt"] for row in rows}
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def spawn_loop(project_dir: Path, dry_run: bool = False) -> None:
    pid_file = project_dir / "forge_loop.pid"
    arch_file = project_dir / "ARCHITECTURE.yaml"

    if not arch_file.exists():
        print(f"[FORGE] ERROR: ARCHITECTURE.yaml not found in {project_dir}. Run architect.py first.")
        sys.exit(1)

    existing_pid = read_pid(pid_file)
    if existing_pid and pid_is_alive(existing_pid):
        print(f"[FORGE] Loop already running for this project (PID: {existing_pid}).")
        print(f"[FORGE] Use --status to check or --stop to terminate.")
        return

    engine = FORGE_ROOT / "engine" / "loop.py"
    if not engine.exists():
        print(f"[FORGE] ERROR: loop.py not found at {engine}")
        sys.exit(1)

    import subprocess
    cmd = [sys.executable, str(engine), "--arch", str(arch_file)]
    if dry_run:
        cmd.append("--dry-run")

    proc = subprocess.Popen(
        cmd,
        cwd=str(project_dir),
        stdout=open(project_dir / "forge_loop.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    pid_file.write_text(str(proc.pid))

    loops = load_active_loops()
    key = get_project_key(project_dir)
    loops[key] = {
        "pid": proc.pid,
        "project_dir": str(project_dir),
        "started_at": now_iso(),
        "log_file": str(project_dir / "forge_loop.log"),
    }
    save_active_loops(loops)

    print(f"[FORGE] Loop started (PID: {proc.pid}).")
    print(f"[FORGE] Updates -> {DEFAULT_RECIPIENT} hourly.")
    print(f"[FORGE] Log: {project_dir / 'forge_loop.log'}")


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def stop_loop(project_dir: Path) -> None:
    pid_file = project_dir / "forge_loop.pid"
    pid = read_pid(pid_file)

    if not pid:
        print("[FORGE] No PID file found — loop may not be running.")
        return

    if not pid_is_alive(pid):
        print(f"[FORGE] PID {pid} is not running. Cleaning up stale PID file.")
        pid_file.unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"[FORGE] Sent SIGTERM to PID {pid}. Loop will stop gracefully.")
    except (OSError, ProcessLookupError) as e:
        print(f"[FORGE] Could not stop PID {pid}: {e}")
        return

    pid_file.unlink(missing_ok=True)

    loops = load_active_loops()
    key = get_project_key(project_dir)
    loops.pop(key, None)
    save_active_loops(loops)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def show_status(project_dir: Path) -> None:
    pid_file = project_dir / "forge_loop.pid"
    pid = read_pid(pid_file)

    if not pid:
        print("[FORGE] No loop running for this project.")
        return

    alive = pid_is_alive(pid)
    status_str = "RUNNING" if alive else "DEAD (stale PID file)"
    print(f"[FORGE] PID:    {pid}")
    print(f"[FORGE] Status: {status_str}")

    loops = load_active_loops()
    key = get_project_key(project_dir)
    info = loops.get(key, {})
    if info:
        started = info.get("started_at", "?")
        print(f"[FORGE] Started: {started}")
        if alive and started and started != "?":
            try:
                from datetime import datetime, timezone
                start_dt = datetime.fromisoformat(started)
                uptime_s = int((datetime.now(timezone.utc) - start_dt).total_seconds())
                h, rem = divmod(uptime_s, 3600)
                m, s = divmod(rem, 60)
                print(f"[FORGE] Uptime:  {h}h {m}m {s}s")
            except Exception:
                pass

    db_path = project_dir / "forge_state.db"
    last_activity = get_last_activity(db_path)
    stats = get_loop_stats(db_path)
    print(f"[FORGE] Last activity: {last_activity}")
    if stats:
        print("[FORGE] Item states:")
        for state, cnt in sorted(stats.items()):
            print(f"  {state}: {cnt}")

    log_file = project_dir / "forge_loop.log"
    if log_file.exists():
        print(f"[FORGE] Log: {log_file} ({log_file.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Start/stop/check the FORGE loop daemon.")
    parser.add_argument("--stop", action="store_true", help="Stop the running loop")
    parser.add_argument("--status", action="store_true", help="Show loop status")
    parser.add_argument("--dry-run", action="store_true", help="Spawn loop in dry-run mode")
    parser.add_argument("--project-dir", default=".", help="Project directory (default: .)")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()

    if args.stop:
        stop_loop(project_dir)
    elif args.status:
        show_status(project_dir)
    else:
        spawn_loop(project_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

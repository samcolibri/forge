#!/usr/bin/env python3
"""
reporter.py — Generate hourly email report + update GitHub Pages dashboard.

Usage:
  python3 reporter.py                   # run report for current project
  python3 reporter.py --all-projects    # report on all active FORGE loops
"""

import argparse
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


FORGE_ROOT = Path.home() / "projects" / "forge"
ACTIVE_LOOPS_FILE = FORGE_ROOT / "state" / "active_loops.json"
DEFAULT_RECIPIENT = "sam.chaudhary@alliedschools.com"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_counts(db_path: Path) -> dict:
    if not db_path.exists():
        return {"error": "DB not found"}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM items GROUP BY state"
        ).fetchall()
        meta_rows = conn.execute("SELECT key, value FROM loop_meta").fetchall()
        gates = conn.execute(
            "SELECT gate_id, item_id, state, created_at FROM human_gates WHERE state='pending'"
        ).fetchall()
        conn.close()

        counts = {row["state"]: row["cnt"] for row in rows}
        meta = {row["key"]: row["value"] for row in meta_rows}
        pending_gates = [
            {"gate_id": g["gate_id"], "item_id": g["item_id"], "created_at": g["created_at"]}
            for g in gates
        ]
        return {"counts": counts, "meta": meta, "pending_gates": pending_gates}
    except sqlite3.Error as e:
        return {"error": str(e)}


def compute_progress(counts: dict, binary_criterion: str) -> str:
    total = sum(counts.values())
    done = counts.get("done", 0)
    failed = counts.get("failed", 0) + counts.get("escalated", 0)
    pending = counts.get("pending_approval", 0)
    processing = counts.get("processing", 0) + counts.get("queued", 0)

    if total == 0:
        return "No items in queue yet"

    pct = int((done / total) * 100) if total > 0 else 0
    return (
        f"{done}/{total} done ({pct}%) | "
        f"{pending} pending approval | "
        f"{processing} in queue | "
        f"{failed} failed/escalated"
    )


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------


def build_email_body(project_dir: Path, db_data: dict, arch: dict) -> tuple[str, str]:
    """Returns (subject, body_markdown)."""
    system = arch.get("system", {})
    system_name = system.get("name", "UNKNOWN")
    outcome = system.get("outcome", "?")
    binary_criterion = system.get("binary_criterion", "?")

    counts = db_data.get("counts", {})
    meta = db_data.get("meta", {})
    pending_gates = db_data.get("pending_gates", [])

    progress = compute_progress(counts, binary_criterion)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    qa_items = counts.get("done", 0) + counts.get("failed", 0) + counts.get("escalated", 0)
    qa_passed = counts.get("done", 0)
    qa_rate = f"{int((qa_passed / qa_items) * 100)}%" if qa_items > 0 else "N/A"

    subject = f"[FORGE/{system_name}] Hourly Report — {ts}"

    lines = [
        f"# FORGE Hourly Report — {system_name}",
        f"**Time:** {ts}",
        f"**Project:** {project_dir}",
        "",
        "## Outcome",
        f"> {outcome}",
        "",
        f"**Binary criterion:** {binary_criterion}",
        "",
        "## Progress",
        f"{progress}",
        "",
        "## State Breakdown",
    ]
    for state, cnt in sorted(counts.items()):
        lines.append(f"- {state}: {cnt}")

    lines += [
        "",
        "## QA",
        f"Pass rate: {qa_rate}",
        "",
    ]

    if pending_gates:
        lines += ["## Pending Human Gates", "The following items are waiting for your approval:", ""]
        for gate in pending_gates:
            lines.append(f"- Gate `{gate['gate_id']}` | Item #{gate['item_id']} | Since {gate['created_at']}")
        lines.append("")

    blockers = []
    if counts.get("escalated", 0) > 0:
        blockers.append(f"{counts['escalated']} items escalated (QA failures exceeded retry limit)")
    if pending_gates:
        blockers.append(f"{len(pending_gates)} items blocked at human gate")
    if db_data.get("error"):
        blockers.append(f"DB error: {db_data['error']}")

    if blockers:
        lines += ["## Blockers", ""]
        for b in blockers:
            lines.append(f"- {b}")
        lines.append("")

    loop_start = meta.get("loop_start", "?")
    stop_reason = meta.get("stop_reason", "")
    lines += [
        "## Loop Info",
        f"- Started: {loop_start}",
    ]
    if stop_reason:
        lines.append(f"- Stopped: {stop_reason}")

    lines += [
        "",
        "---",
        "*Sent by FORGE reporter.py*",
    ]

    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------


def send_email(subject: str, body_md: str, recipient: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    if not smtp_host or not smtp_user:
        print(f"[FORGE] WARNING: SMTP_HOST or SMTP_USER not set — skipping email send")
        print(f"[FORGE] Email subject: {subject}")
        print(f"[FORGE] Recipient: {recipient}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient

    text_part = MIMEText(body_md, "plain")
    msg.attach(text_part)

    try:
        import html
        html_body = "<pre style='font-family:monospace;'>" + html.escape(body_md) + "</pre>"
        html_part = MIMEText(html_body, "html")
        msg.attach(html_part)
    except Exception:
        pass

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipient, msg.as_string())
        print(f"[FORGE] Email sent to {recipient}: {subject}")
        return True
    except smtplib.SMTPException as e:
        print(f"[FORGE] SMTP error: {e}")
        return False


# ---------------------------------------------------------------------------
# Dashboard update
# ---------------------------------------------------------------------------


def update_dashboard(project_dir: Path, db_data: dict, arch: dict) -> bool:
    """Update GitHub Pages index.html with current progress numbers."""
    index_html = project_dir / "index.html"
    if not index_html.exists():
        index_html = FORGE_ROOT / "index.html"
    if not index_html.exists():
        print("[FORGE] No index.html found — skipping dashboard update")
        return False

    counts = db_data.get("counts", {})
    done = counts.get("done", 0)
    total = sum(counts.values())
    failed = counts.get("failed", 0) + counts.get("escalated", 0)
    pending = counts.get("pending_approval", 0)
    pct = int((done / total) * 100) if total > 0 else 0
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    content = index_html.read_text()
    replacements = [
        ("<!-- FORGE_DONE -->", str(done)),
        ("<!-- FORGE_TOTAL -->", str(total)),
        ("<!-- FORGE_PCT -->", f"{pct}%"),
        ("<!-- FORGE_FAILED -->", str(failed)),
        ("<!-- FORGE_PENDING -->", str(pending)),
        ("<!-- FORGE_UPDATED -->", ts),
    ]
    for placeholder, value in replacements:
        content = content.replace(placeholder, value)

    index_html.write_text(content)

    repo_dir = project_dir if (project_dir / ".git").exists() else FORGE_ROOT
    if not (repo_dir / ".git").exists():
        print("[FORGE] No git repo found — skipping dashboard commit")
        return False

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "add", str(index_html)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[FORGE] git add failed: {result.stderr}")
            return False
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m",
             f"chore: FORGE dashboard update {ts}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[FORGE] git commit: {result.stderr}")
            return False
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "push"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[FORGE] git push failed: {result.stderr}")
            return False
        print(f"[FORGE] Dashboard updated and pushed to GitHub Pages.")
        return True
    except FileNotFoundError:
        print("[FORGE] git not found — skipping dashboard commit")
        return False


# ---------------------------------------------------------------------------
# Report runner
# ---------------------------------------------------------------------------


def run_report_for_project(project_dir: Path) -> None:
    db_path = project_dir / "forge_state.db"
    arch_path = project_dir / "ARCHITECTURE.yaml"

    if not arch_path.exists():
        print(f"[FORGE] WARNING: ARCHITECTURE.yaml not found in {project_dir} — skipping")
        return

    try:
        import yaml
        arch = yaml.safe_load(arch_path.read_text()) or {}
    except Exception as e:
        print(f"[FORGE] WARNING: Could not read ARCHITECTURE.yaml: {e}")
        arch = {}

    db_data = get_counts(db_path)
    system = arch.get("system", {})
    binary_criterion = system.get("binary_criterion", "")

    reporting = arch.get("reporting", {})
    recipients = reporting.get("recipients", [DEFAULT_RECIPIENT])
    if not recipients:
        recipients = [DEFAULT_RECIPIENT]

    subject, body = build_email_body(project_dir, db_data, arch)

    for recipient in recipients:
        send_email(subject, body, str(recipient))

    update_dashboard(project_dir, db_data, arch)
    print(f"[FORGE] Report complete for {project_dir}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate FORGE hourly report.")
    parser.add_argument("--all-projects", action="store_true", help="Report on all active FORGE loops")
    parser.add_argument("--project-dir", default=".", help="Project directory (default: .)")
    args = parser.parse_args()

    if args.all_projects:
        if not ACTIVE_LOOPS_FILE.exists():
            print("[FORGE] No active loops found.")
            return
        loops = json.loads(ACTIVE_LOOPS_FILE.read_text())
        if not loops:
            print("[FORGE] No active loops registered.")
            return
        for key, info in loops.items():
            project_dir = Path(info.get("project_dir", key))
            print(f"[FORGE] Reporting for: {project_dir}")
            run_report_for_project(project_dir)
    else:
        project_dir = Path(args.project_dir).resolve()
        run_report_for_project(project_dir)

    print("[FORGE] Report sent. Dashboard updated.")


if __name__ == "__main__":
    main()

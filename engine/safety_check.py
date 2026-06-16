#!/usr/bin/env python3
"""
safety_check.py — Public safety gate before any FORGE output leaves the system.

Mirrors Hephaestus's public_safety_check.sh as Python so it can be called
programmatically from the spawner and the Gate 2 review step.

FORGE calls scan_output() or scan_batch() before every Gate 2 release.
If safe=False, the Gate 2 workflow halts and notifies the owner.

Usage:
  from engine.safety_check import scan_output, scan_file, scan_batch
  result = scan_output(content)
  if not result["safe"]:
      raise RuntimeError(f"Safety violations: {result['violations']}")

CLI:
  python3 safety_check.py path/to/file.txt [path/to/file2.txt ...]
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Blocked patterns — extend this list, never shrink it
# ---------------------------------------------------------------------------

# Each entry: (human_label, compiled_regex)
_RAW_PATTERNS: list[tuple[str, str]] = [
    ("anthropic_api_key",    r"sk-ant-[a-zA-Z0-9\-]{10,}"),
    ("github_pat_classic",   r"ghp_[a-zA-Z0-9]{36,}"),
    ("github_pat_fine",      r"github_pat_[a-zA-Z0-9_]{82,}"),
    ("airtable_token",       r"patAKZ[a-zA-Z0-9.]{10,}"),
    ("gitlab_token",         r"glpat-[a-zA-Z0-9\-]{20,}"),
    ("aws_access_key",       r"AKIA[A-Z0-9]{16}"),
    ("aws_secret_key",       r"(?i)aws.{0,20}secret.{0,10}=\s*[A-Za-z0-9/+=]{40}"),
    ("openai_api_key",       r"sk-[a-zA-Z0-9]{48,}"),
    ("anthropic_admin_key",  r"sk-ant-admin[a-zA-Z0-9\-]{5,}"),
    ("sendgrid_key",         r"SG\.[a-zA-Z0-9\-_]{22}\.[a-zA-Z0-9\-_]{43}"),
    ("stripe_key",           r"(?:sk|pk)_(?:test|live)_[a-zA-Z0-9]{24,}"),
    ("twilio_sid",           r"AC[a-zA-Z0-9]{32}"),
    ("mac_home_path",        r"/Users/[a-zA-Z0-9_\-]+/"),
    ("linux_home_path",      r"/home/[a-zA-Z0-9_\-]+/"),
    ("hardcoded_password",   r'"password"\s*:\s*"[^"]{4,}"'),
    ("dotenv_reference",     r"\.env(?:\b|$)"),
    ("private_key_block",    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY"),
    ("bearer_token",         r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9\-_=]{20,}"),
]

# Compile once at import time for performance
BLOCKED_PATTERNS: list[tuple[str, re.Pattern]] = [
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in _RAW_PATTERNS
]


# ---------------------------------------------------------------------------
# Core scanners
# ---------------------------------------------------------------------------


def scan_output(content: str, output_type: str = "text") -> dict:
    """
    Scan any string content for safety violations before release.

    Args:
        content:     The text content to scan (email body, JSON payload, etc.).
        output_type: Human-readable label for the content type (e.g. "email",
                     "json", "markdown").  Stored in violation records for
                     traceability.

    Returns:
        {
          "safe":          bool  — True iff no violations found,
          "violations":    list  — [{label, match, line, output_type}],
          "scanned_lines": int   — total lines scanned,
          "output_type":   str,
        }
        Matched secrets are NEVER stored in violations["match"] — only
        "[REDACTED]" appears there.
    """
    violations: list[dict] = []
    lines = content.split("\n")

    for line_num, line in enumerate(lines, start=1):
        for label, pattern in BLOCKED_PATTERNS:
            if pattern.search(line):
                violations.append({
                    "label": label,
                    "match": "[REDACTED]",   # never log the actual secret
                    "line": line_num,
                    "output_type": output_type,
                })

    return {
        "safe": len(violations) == 0,
        "violations": violations,
        "scanned_lines": len(lines),
        "output_type": output_type,
    }


def scan_file(filepath: str) -> dict:
    """
    Scan a single file before it leaves the system.

    Args:
        filepath: Absolute or relative path to the file.

    Returns:
        Same schema as scan_output().  On read error, safe=False with a
        synthetic violation record describing the failure.
    """
    try:
        with open(filepath, "r", errors="replace") as fh:
            content = fh.read()
        return scan_output(content, output_type=os.path.basename(filepath))
    except OSError as e:
        return {
            "safe": False,
            "violations": [{"label": "read_error", "match": str(e), "line": 0,
                            "output_type": os.path.basename(filepath)}],
            "scanned_lines": 0,
            "output_type": os.path.basename(filepath),
        }


def scan_batch(filepaths: list[str]) -> dict:
    """
    Scan multiple output files and aggregate results.

    Stops collecting violations after the first 50 across all files to avoid
    unbounded output in pathological cases.

    Args:
        filepaths: List of file paths to scan.

    Returns:
        {
          "safe":          bool,
          "violations":    list  — merged from all files,
          "files_scanned": int,
          "files_with_violations": int,
        }
    """
    all_violations: list[dict] = []
    files_with_violations = 0
    max_violations = 50

    for fp in filepaths:
        result = scan_file(fp)
        if not result["safe"]:
            files_with_violations += 1
            remaining = max_violations - len(all_violations)
            all_violations.extend(result["violations"][:remaining])
            if len(all_violations) >= max_violations:
                break

    return {
        "safe": len(all_violations) == 0,
        "violations": all_violations,
        "files_scanned": len(filepaths),
        "files_with_violations": files_with_violations,
    }


# ---------------------------------------------------------------------------
# Gate 2 helper — called by spawner.py
# ---------------------------------------------------------------------------


def gate2_check(output_dir: str, file_patterns: Optional[list[str]] = None) -> dict:
    """
    Run a full Gate 2 safety sweep on all outputs in a directory.

    Walks output_dir, collects files matching any of the given glob patterns
    (default: all .txt, .json, .md, .yaml, .html, .csv files), scans them,
    and returns a structured gate result.

    Args:
        output_dir:    Directory containing Gate 2 candidate output files.
        file_patterns: Optional list of glob patterns (e.g. ["*.json", "*.md"]).
                       Defaults to ["*.txt","*.json","*.md","*.yaml","*.html","*.csv"].

    Returns:
        {
          "gate": "gate_2",
          "approved": bool,
          "result": scan_batch() result dict,
          "output_dir": str,
        }
    """
    if file_patterns is None:
        file_patterns = ["*.txt", "*.json", "*.md", "*.yaml", "*.html", "*.csv"]

    output_path = Path(output_dir)
    filepaths: list[str] = []
    for pattern in file_patterns:
        filepaths.extend(str(p) for p in output_path.glob(pattern))

    batch_result = scan_batch(filepaths)
    return {
        "gate": "gate_2",
        "approved": batch_result["safe"],
        "result": batch_result,
        "output_dir": output_dir,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli_main() -> None:
    """Scan files passed as CLI arguments and print a JSON report."""
    if len(sys.argv) < 2:
        print("Usage: safety_check.py <file> [file2 ...]", file=sys.stderr)
        sys.exit(1)

    filepaths = sys.argv[1:]
    result = scan_batch(filepaths)
    print(json.dumps(result, indent=2))

    if not result["safe"]:
        count = len(result["violations"])
        print(f"\n[safety_check] BLOCKED — {count} violation(s) found in "
              f"{result['files_with_violations']} file(s).", file=sys.stderr)
        sys.exit(2)
    else:
        print(f"\n[safety_check] OK — {result['files_scanned']} file(s) scanned, no violations.")


if __name__ == "__main__":
    _cli_main()

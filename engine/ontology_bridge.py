#!/usr/bin/env python3
"""
ontology_bridge.py — Query the Hephaestus local ontology before external search.

The SCOUT worker calls this first: if ontology returns a high-confidence answer,
skip the Exa web search. Only call Exa for facts not found locally.

Usage:
  from engine.ontology_bridge import query_ontology, ingest_project_sources, get_project_context
"""

import json
import os
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONTOLOGY_BIN = os.path.expanduser("~/.agentlas/runtime/current/bin/ontology")
CONFIDENCE_THRESHOLD = 0.7


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------


def query_ontology(query: str, worker_name: str, project_dir: str) -> dict:
    """
    Query the local Hephaestus ontology runtime.

    Calls the agentlas ontology binary with the given query in JSON output mode.
    Returns immediately with found=False if the runtime or DB is not present,
    so callers can fall through to external search without raising.

    Args:
        query:        Natural-language or keyword query string.
        worker_name:  Name of the calling worker (e.g. "SCOUT"), used for
                      per-worker scope filtering inside the ontology.
        project_dir:  Absolute path to the FORGE project directory; the ontology
                      DB lives at <project_dir>/.agentlas/ontology-runtime.sqlite.

    Returns:
        {
          "found":       bool   — True iff confidence >= 0.7,
          "confidence":  float  — 0.0–1.0 from the ontology,
          "chunks":      list   — matching text chunks [{text, source, score}],
          "entities":    list   — named entities extracted from chunks,
          "source_refs": list   — canonical source references,
        }
        On failure adds "reason": str explaining why found=False.
    """
    if not os.path.exists(ONTOLOGY_BIN):
        return {"found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": "ontology_runtime_not_installed"}

    ontology_db = os.path.join(project_dir, ".agentlas", "ontology-runtime.sqlite")
    if not os.path.exists(ontology_db):
        return {"found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": "ontology_not_yet_indexed"}

    try:
        result = subprocess.run(
            [ONTOLOGY_BIN, "query", query, "--agent", worker_name, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_dir,
        )

        if result.returncode != 0:
            stderr_snippet = result.stderr.strip()[:200] if result.stderr else ""
            return {
                "found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": f"ontology_exit_{result.returncode}: {stderr_snippet}",
            }

        data = json.loads(result.stdout)
        confidence = float(data.get("confidence", 0.0))
        return {
            "found": confidence >= CONFIDENCE_THRESHOLD,
            "confidence": confidence,
            "chunks": data.get("chunks", []),
            "entities": data.get("entities", []),
            "source_refs": data.get("source_refs", []),
        }

    except json.JSONDecodeError as e:
        return {"found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": f"json_parse_error: {e}"}
    except subprocess.TimeoutExpired:
        return {"found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": "ontology_query_timeout"}
    except Exception as e:  # noqa: BLE001
        return {"found": False, "confidence": 0.0, "chunks": [], "entities": [],
                "source_refs": [], "reason": str(e)}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_project_sources(project_dir: str) -> bool:
    """
    Ingest project sources into the local ontology.

    Reads .agentlas/ontology-sources.json from project_dir and passes all
    declared internal sources to the ontology runtime for indexing.  Intended
    to be called once at FORGE project startup (before the first worker run).

    Args:
        project_dir: Absolute path to the FORGE project root.

    Returns:
        True if the ontology binary exited 0, False otherwise.
    """
    if not os.path.exists(ONTOLOGY_BIN):
        print("[ontology_bridge] WARNING: agentlas runtime not installed — skipping ingest.")
        return False

    sources_config = os.path.join(project_dir, ".agentlas", "ontology-sources.json")
    if not os.path.exists(sources_config):
        print(f"[ontology_bridge] WARNING: {sources_config} not found — skipping ingest.")
        return False

    try:
        result = subprocess.run(
            [ONTOLOGY_BIN, "ingest", ".", "--scope", "internal"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        success = result.returncode == 0
        if not success:
            print(f"[ontology_bridge] Ingest failed (exit {result.returncode}): "
                  f"{result.stderr.strip()[:300]}")
        else:
            print("[ontology_bridge] Ontology ingest complete.")
        return success

    except subprocess.TimeoutExpired:
        print("[ontology_bridge] ERROR: ontology ingest timed out after 120s.")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[ontology_bridge] ERROR during ingest: {e}")
        return False


# ---------------------------------------------------------------------------
# Context loader
# ---------------------------------------------------------------------------


def get_project_context(project_dir: str) -> str:
    """
    Return a summary of what the ontology knows about this project.

    Queries for high-level project concepts (outcome, binary criterion, system
    overview) and returns up to five text chunks joined by newlines.  Used by
    BRIEFER and SCOUT to pre-load project context before spawning external
    searches.

    Args:
        project_dir: Absolute path to the FORGE project root.

    Returns:
        A newline-separated string of relevant ontology chunks, or "" if
        the ontology is unavailable or confidence is below threshold.
    """
    result = query_ontology(
        "project overview outcome binary criterion system goal",
        "context_loader",
        project_dir,
    )
    if result.get("found"):
        chunks = result.get("chunks", [])
        return "\n".join(c.get("text", "") for c in chunks[:5] if c.get("text"))
    return ""


# ---------------------------------------------------------------------------
# Stormbreaker evidence helper
# ---------------------------------------------------------------------------


def build_evidence_record(query: str, ontology_result: dict) -> dict:
    """
    Convert an ontology result into a Stormbreaker-compatible evidence record.

    Stormbreaker's evidence_loop requires every fact to carry a source_url
    and a confidence_score >= 0.7.  This helper packages ontology hits into
    that format so SCOUT can forward them directly to Stormbreaker.

    Args:
        query:           The original query string that produced the result.
        ontology_result: Return value from query_ontology().

    Returns:
        A dict with keys: query, source_url, confidence_score, chunks, entities.
        source_url is constructed from source_refs when present.
    """
    source_refs = ontology_result.get("source_refs", [])
    source_url = source_refs[0] if source_refs else "ontology://local"

    return {
        "query": query,
        "source_url": source_url,
        "confidence_score": ontology_result.get("confidence", 0.0),
        "chunks": ontology_result.get("chunks", []),
        "entities": ontology_result.get("entities", []),
        "from_ontology": True,
    }


# ---------------------------------------------------------------------------
# CLI shim for quick debugging
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Query the Hephaestus ontology for a FORGE project directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", help="Query string to send to the ontology")
    parser.add_argument("--worker", default="SCOUT", help="Worker name (default: SCOUT)")
    parser.add_argument("--project-dir", default=".", help="FORGE project root (default: .)")
    parser.add_argument("--ingest", action="store_true", help="Run ingest before querying")
    args = parser.parse_args()

    project_dir = str(Path(args.project_dir).resolve())

    if args.ingest:
        ok = ingest_project_sources(project_dir)
        print(f"[ontology_bridge] Ingest {'succeeded' if ok else 'failed'}.")
        if not ok:
            sys.exit(1)

    result = query_ontology(args.query, args.worker, project_dir)
    print(json.dumps(result, indent=2))

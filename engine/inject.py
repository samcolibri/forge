#!/usr/bin/env python3
"""
inject.py — Convert any outcome statement into a structured FORGE fable prompt.

Usage:
  python3 inject.py "book 5 meetings for Moreland school districts"
  python3 inject.py --from-file outcome.txt
  python3 inject.py --interactive
"""

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORGE_ROOT = Path.home() / "projects" / "forge"
BASE_FABLE_PATH = FORGE_ROOT / "prompts" / "BASE_FABLE.md"
DEFAULT_RECIPIENT = "sam.chaudhary@alliedschools.com"
MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_base_fable() -> str:
    if not BASE_FABLE_PATH.exists():
        print(f"[FORGE] ERROR: BASE_FABLE.md not found at {BASE_FABLE_PATH}", file=sys.stderr)
        sys.exit(1)
    return BASE_FABLE_PATH.read_text()


def get_outcome(args: argparse.Namespace) -> str:
    if args.from_file:
        path = Path(args.from_file)
        if not path.exists():
            print(f"[FORGE] ERROR: File not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text().strip()
    if args.interactive:
        print("[FORGE] Enter your outcome statement (press Enter twice when done):")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        return "\n".join(lines).strip()
    if args.outcome:
        return " ".join(args.outcome)
    print("[FORGE] ERROR: No outcome provided. Use positional arg, --from-file, or --interactive.", file=sys.stderr)
    sys.exit(1)


def build_system_prompt(base_fable: str) -> str:
    return textwrap.dedent(f"""
        You are FORGE ARCHITECT, an AI that converts outcome statements into structured autonomous agent fables.

        Given an outcome statement, you must fill in a fable template with the following fields:

        - SYSTEM_NAME: A 4-letter uppercase code that captures the mission (e.g., SCOT for scouting, BLDX for build, RSCH for research).
        - OUTCOME_STATEMENT: The original outcome, cleaned up into a crisp single sentence.
        - BINARY_CRITERION: A precise, measurable yes/no test for when the outcome is achieved (e.g., "5 calendar invites accepted by Moreland district contacts").
        - WORKER_FLEET_LIST: A markdown bulleted list of worker agents inferred from the outcome type.
          Infer workers by pattern:
          - "book meetings / outreach / SDR" → SCOUT, ENRICHER, WRITER, VALIDATOR, QA_SCORER
          - "build a feature / code" → PLANNER, CODER, TESTER, REVIEWER
          - "research a market / analyze" → RESEARCHER, SYNTHESIZER, REPORTER
          - "content / write / publish" → BRIEFER, DRAFTER, EDITOR, PUBLISHER
          - "data / pipeline / ETL" → EXTRACTOR, TRANSFORMER, LOADER, VALIDATOR
          - Mix and match as needed. Each bullet: `- **WORKER_NAME** — one-line description of its job`
        - LOOP_STOP_CONDITION: 2-4 bullet points listing when the loop stops (outcome met, max iterations, human stop, error threshold).
        - HUMAN_GATE_1: The first human approval checkpoint (e.g., "Review first 5 drafted emails before any sends").
        - HUMAN_GATE_2: The second human approval checkpoint (e.g., "Approve final batch before export to CRM").
        - REPORT_RECIPIENT: Use {DEFAULT_RECIPIENT} unless context implies otherwise.

        Here is the BASE_FABLE.md template you must fill:

        ---BEGIN TEMPLATE---
        {base_fable}
        ---END TEMPLATE---

        RULES:
        1. Replace every {{{{PLACEHOLDER}}}} with the correct value.
        2. Keep the full template structure — do not remove any sections.
        3. WORKER_FLEET_LIST should be 3-6 workers, each a markdown bullet.
        4. BINARY_CRITERION must be a single testable sentence ending in a number or measurable state.
        5. Return ONLY the filled fable markdown. No preamble, no commentary, no code fences.
    """).strip()


def build_meta_prompt(outcome: str, filled_fable: str) -> str:
    return textwrap.dedent(f"""
        You are FORGE METADATA EXTRACTOR.

        Given an outcome statement and a filled fable, extract the following fields as a JSON object:

        - system_name: string (4-letter code from the fable)
        - outcome: string (cleaned outcome statement)
        - binary_criterion: string (the measurable stop condition)
        - worker_types_needed: array of strings (worker names from the fleet list)
        - estimated_iterations: integer (rough estimate of how many loop cycles needed, based on the scale of the task)
        - project_dir: string (use "." as the current project directory placeholder)

        Outcome statement:
        {outcome}

        Filled fable:
        {filled_fable[:8000]}

        Return ONLY valid JSON. No markdown, no preamble.
    """).strip()


def call_claude(client: anthropic.Anthropic, system: str, user: str) -> str:
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert an outcome statement into a FORGE fable prompt.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python3 inject.py "book 5 meetings for Moreland school districts"
              python3 inject.py --from-file outcome.txt
              python3 inject.py --interactive
        """),
    )
    parser.add_argument("outcome", nargs="*", help="Outcome statement (positional)")
    parser.add_argument("--from-file", metavar="PATH", help="Read outcome from file")
    parser.add_argument("--interactive", action="store_true", help="Enter outcome interactively")
    parser.add_argument("--output-dir", default=".", help="Directory to write OUTCOME.md and outcome_meta.json (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="Print filled fable to stdout without writing files")
    args = parser.parse_args()

    # Load API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[FORGE] WARNING: ANTHROPIC_API_KEY not set. Cannot call Claude.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    base_fable_raw = load_base_fable()
    # Strip the template meta-header block (lines before the first `---` divider)
    # so Claude receives only the fable structure, not file documentation
    if '---\n' in base_fable_raw:
        base_fable = base_fable_raw[base_fable_raw.index('---\n'):]
    else:
        base_fable = base_fable_raw
    outcome = get_outcome(args)

    print(f"[FORGE] Injecting outcome: {outcome[:80]}{'...' if len(outcome) > 80 else ''}")
    print("[FORGE] Calling Claude to fill fable template...")

    # Step 1: Fill the fable template
    system_prompt = build_system_prompt(base_fable)
    filled_fable = call_claude(client, system_prompt, f"Outcome: {outcome}")

    # Post-process: substitute any unfilled template variables
    # {PROJECT_REPO} is left for the user to fill — substitute a sensible default
    import re as _re
    repo_guess = Path(args.output_dir).resolve().name
    filled_fable = filled_fable.replace("{PROJECT_REPO}", f"samcolibri/{repo_guess}")
    # Strip any remaining unfilled ALL_CAPS placeholders (shouldn't happen but be safe)
    filled_fable = _re.sub(r'\{[A-Z_]{4,}\}', '[TBD]', filled_fable)

    print("[FORGE] Fable filled. Extracting metadata...")

    # Step 2: Extract machine-readable metadata
    meta_system = "You are a precise JSON extractor. Return only valid JSON."
    meta_prompt = build_meta_prompt(outcome, filled_fable)
    meta_raw = call_claude(client, meta_system, meta_prompt)

    # Parse metadata with fallback
    try:
        # Strip any accidental markdown fences
        clean_meta = meta_raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        meta = json.loads(clean_meta)
    except json.JSONDecodeError as e:
        print(f"[FORGE] WARNING: Could not parse metadata JSON: {e}", file=sys.stderr)
        meta = {
            "system_name": "UNKN",
            "outcome": outcome,
            "binary_criterion": "manual review required",
            "worker_types_needed": [],
            "estimated_iterations": 10,
            "project_dir": ".",
        }

    if args.dry_run:
        print("\n" + "=" * 60)
        print("FILLED FABLE (dry-run — not written to disk):")
        print("=" * 60)
        print(filled_fable)
        print("\n" + "=" * 60)
        print("METADATA:")
        print("=" * 60)
        print(json.dumps(meta, indent=2))
        return

    # Step 3: Write files
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    outcome_path = output_dir / "OUTCOME.md"
    meta_path = output_dir / "outcome_meta.json"

    outcome_path.write_text(filled_fable)
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"[FORGE] OUTCOME.md written to {outcome_path.resolve()}")
    print(f"[FORGE] outcome_meta.json written to {meta_path.resolve()}")
    print(f"[FORGE] System: {meta.get('system_name', '?')} | Workers: {', '.join(meta.get('worker_types_needed', []))}")
    print(f"[FORGE] Next step: python3 ~/projects/forge/engine/architect.py")


if __name__ == "__main__":
    main()

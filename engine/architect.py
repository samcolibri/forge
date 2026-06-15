#!/usr/bin/env python3
"""
architect.py — Design worker fleet from fable prompt.

Usage:
  python3 architect.py                    # reads ./OUTCOME.md
  python3 architect.py --outcome path     # reads specific file
  python3 architect.py --print-only       # print architecture, don't write files
"""

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import anthropic
import yaml

MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_inputs(outcome_path: Path, meta_path: Path) -> tuple[str, dict]:
    if not outcome_path.exists():
        print(f"[FORGE] ERROR: {outcome_path} not found. Run inject.py first.", file=sys.stderr)
        sys.exit(1)

    outcome_md = outcome_path.read_text()
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            print("[FORGE] WARNING: outcome_meta.json is malformed. Proceeding without it.", file=sys.stderr)
    return outcome_md, meta


def build_architecture_prompt(outcome_md: str, meta: dict) -> str:
    worker_hint = ", ".join(meta.get("worker_types_needed", [])) or "infer from the fable"
    return textwrap.dedent(f"""
        You are FORGE ARCHITECT. Generate a complete ARCHITECTURE.yaml for an autonomous agent system.

        The fable (OUTCOME.md) defines the system. Here it is:

        ---BEGIN FABLE---
        {outcome_md}
        ---END FABLE---

        Known metadata: {json.dumps(meta)}

        Generate a YAML document with these exact top-level keys:

        system:
          name: (4-letter code)
          version: "1.0"
          outcome: (outcome statement)
          binary_criterion: (measurable stop condition)
          owner: (report recipient email)

        loop:
          mode: continuous
          batch_size: 10
          stop_conditions:
            - outcome_met: (describe)
            - max_iterations: 100
            - human_stop: true
            - error_threshold: 0.3
          state_store: ./forge_state.db

        workers:
          - name: WORKER_NAME
            role: (one-line role description)
            model: claude-sonnet-4-6   # or gpt-4o, gemini-pro, bash, http
            dispatch: claude_api       # one of: claude_api, bash, http
            tools: []                  # list of tool names (e.g., web_search, send_email)
            system_prompt: |
              (2-4 sentence system prompt for this worker)
            inputs:
              - (field name from previous worker or source)
            outputs:
              - (field name this worker produces)
            success_criteria: (one-line measurable criteria)
            failure_handling: retry_with_feedback  # or skip, escalate, halt
          # repeat for each worker in the fleet: {worker_hint}

        human_gates:
          - id: gate_1
            trigger_state: (state that triggers this gate)
            description: (what the human reviews)
            approver: (email or role)
          - id: gate_2
            trigger_state: (state that triggers this gate)
            description: (what the human reviews)
            approver: (email or role)

        integrations:
          # list services inferred from worker tools (e.g., smtp, anthropic, serpapi)
          - name: anthropic
            env_var: ANTHROPIC_API_KEY
            required: true
          - name: smtp
            env_var: SMTP_HOST
            required: false

        qa:
          rubric:
            - criterion: relevance
              weight: 0.3
            - criterion: accuracy
              weight: 0.4
            - criterion: completeness
              weight: 0.3
          threshold: 0.75
          max_retries: 2
          scorer_model: claude-sonnet-4-6

        reporting:
          cadence_seconds: 3600
          recipients:
            - (owner email)
          include_pending_gates: true
          include_blockers: true

        RULES:
        1. Return ONLY valid YAML. No markdown fences, no preamble.
        2. Use real worker names from the fable (SCOUT, ENRICHER, etc.) or infer sensible ones.
        3. Each worker must have all fields shown above.
        4. Tools should be realistic (web_search, send_email, crm_lookup, code_exec, file_write).
        5. System prompts should be actionable and specific to the outcome.
    """).strip()


def build_worker_contract_prompt(worker: dict, system_meta: dict) -> str:
    return textwrap.dedent(f"""
        You are FORGE CONTRACT WRITER. Write a detailed YAML contract for a single FORGE worker.

        System context: {json.dumps(system_meta)}
        Worker definition: {json.dumps(worker)}

        Generate a YAML document with these keys:

        worker:
          name: {worker.get('name', 'WORKER')}
          version: "1.0"
          role: (expanded role description — 2 sentences)
          model: (model id)
          dispatch: (claude_api | bash | http)

        prompt:
          system: |
            (full system prompt — 4-8 sentences, specific to this worker's job and the overall outcome)
          input_schema:
            # describe expected input fields with types and descriptions
          output_schema:
            # describe output fields with types and descriptions

        behavior:
          on_success: pass_to_next
          on_failure: {worker.get('failure_handling', 'retry_with_feedback')}
          max_retries: 2
          retry_prompt: |
            (instructions to give the worker when retrying after failure)

        quality:
          success_criteria: {worker.get('success_criteria', 'output meets requirements')}
          self_check: |
            (instruction for the worker to self-evaluate before passing output)

        tools:
          # each tool with: name, description, when_to_use
          {chr(10).join(['- name: ' + t for t in (worker.get('tools') or ['none'])])}

        RULES: Return ONLY valid YAML. No markdown, no preamble.
    """).strip()


def print_summary_table(workers: list[dict]) -> None:
    header = f"{'WORKER':<18} {'ROLE':<30} {'MODEL':<22} {'KEY TOOLS':<30}"
    print("\n" + "=" * len(header))
    print("FORGE WORKER FLEET")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for w in workers:
        name = str(w.get("name", "?"))[:17]
        role = str(w.get("role", "?"))[:29]
        model = str(w.get("model", "?"))[:21]
        tools = ", ".join(w.get("tools") or [])[:29]
        print(f"{name:<18} {role:<30} {model:<22} {tools:<30}")
    print("=" * len(header) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Design a FORGE worker fleet from a fable prompt.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--outcome", default="./OUTCOME.md", help="Path to OUTCOME.md (default: ./OUTCOME.md)")
    parser.add_argument("--print-only", action="store_true", help="Print architecture YAML to stdout, don't write files")
    parser.add_argument("--output-dir", default=".", help="Directory to write ARCHITECTURE.yaml and workers/ (default: .)")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[FORGE] ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    outcome_path = Path(args.outcome)
    meta_path = outcome_path.parent / "outcome_meta.json"
    outcome_md, meta = load_inputs(outcome_path, meta_path)

    print("[FORGE] Calling Claude to design architecture...")
    arch_prompt = build_architecture_prompt(outcome_md, meta)

    message = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        system="You are FORGE ARCHITECT. Return only valid YAML without any markdown fences or commentary.",
        messages=[{"role": "user", "content": arch_prompt}],
    )
    arch_yaml_raw = message.content[0].text.strip()

    # Strip accidental fences
    if arch_yaml_raw.startswith("```"):
        lines = arch_yaml_raw.split("\n")
        arch_yaml_raw = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    # Validate YAML
    try:
        arch_data = yaml.safe_load(arch_yaml_raw)
    except yaml.YAMLError as e:
        print(f"[FORGE] ERROR: Generated YAML is invalid: {e}", file=sys.stderr)
        print("[FORGE] Raw output:\n", arch_yaml_raw[:500], file=sys.stderr)
        sys.exit(1)

    workers = arch_data.get("workers", [])
    print_summary_table(workers)

    if args.print_only:
        print(arch_yaml_raw)
        return

    # Write files
    output_dir = Path(args.output_dir)
    workers_dir = output_dir / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)

    arch_path = output_dir / "ARCHITECTURE.yaml"
    arch_path.write_text(arch_yaml_raw)
    print(f"[FORGE] ARCHITECTURE.yaml written to {arch_path.resolve()}")

    # Generate individual worker contracts
    system_meta = arch_data.get("system", {})
    for worker in workers:
        worker_name = str(worker.get("name", "worker")).lower()
        print(f"[FORGE] Generating contract for {worker_name.upper()}...")

        contract_prompt = build_worker_contract_prompt(worker, system_meta)
        contract_msg = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system="You are FORGE CONTRACT WRITER. Return only valid YAML without markdown fences.",
            messages=[{"role": "user", "content": contract_prompt}],
        )
        contract_raw = contract_msg.content[0].text.strip()
        if contract_raw.startswith("```"):
            lines = contract_raw.split("\n")
            contract_raw = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        worker_file = workers_dir / f"{worker_name}.yaml"
        worker_file.write_text(contract_raw)
        print(f"[FORGE] Contract written: workers/{worker_name}.yaml")

    print(f"\n[FORGE] Architecture complete. {len(workers)} workers designed.")
    print(f"[FORGE] Next step: python3 ~/projects/forge/engine/spawner.py")


if __name__ == "__main__":
    main()

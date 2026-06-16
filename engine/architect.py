#!/usr/bin/env python3
"""
architect.py — Design worker fleet from fable prompt.

Usage:
  python3 architect.py                    # reads ./OUTCOME.md
  python3 architect.py --outcome path     # reads specific file
  python3 architect.py --print-only       # print architecture, don't write files
  python3 architect.py --output-dir path  # write artefacts to a different directory
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

        routing_card:
          triggers:
            # 2-4 short phrases that describe when a router should send work to this worker
          anti_triggers:
            # 2-4 short phrases describing when NOT to route here (tasks this worker must NOT handle)
          capabilities:
            # list of capability identifiers this worker provides (e.g. web_search, contact_discovery)
          risk_profile: (low | medium | high)
          memory_behavior:
            reads:
              # list of memory/cache keys this worker reads (or [] if none)
            writes:
              # list of output artifacts this worker writes (e.g. account_brief.json)
            durable_writes: false   # set true only for workers that need Curator approval
          evidence_requirement:
            # Stormbreaker requirements: list what each produced fact must include
            - "source_url for every fact"
            - "confidence_score >= 0.7"

        RULES: Return ONLY valid YAML. No markdown, no preamble.
    """).strip()


# ---------------------------------------------------------------------------
# Hephaestus integration helpers
# ---------------------------------------------------------------------------


def _infer_risk_tier(worker: dict) -> str:
    """
    Heuristically assign a Stormbreaker risk tier (low / medium / high) based
    on the worker's declared tools and role keywords.

    Rules (first match wins):
    - high:   any of send_email, send_sms, crm_update, crm_write, post_webhook,
              execute_code, code_exec, db_write
    - medium: any of web_search, http_request, crm_lookup, file_write, api_call
    - low:    everything else (read-only, local computation)
    """
    high_tools = {"send_email", "send_sms", "crm_update", "crm_write",
                  "post_webhook", "execute_code", "code_exec", "db_write"}
    medium_tools = {"web_search", "http_request", "crm_lookup", "file_write", "api_call"}

    tools = {str(t).lower() for t in (worker.get("tools") or [])}
    if tools & high_tools:
        return "high"
    if tools & medium_tools:
        return "medium"
    return "low"


def generate_agents_md(arch_data: dict, output_dir: Path) -> Path:
    """
    Write AGENTS.md to output_dir.

    This is the Hephaestus standard contract file that lets FORGE projects work
    with Claude Code, Codex, Gemini CLI, Cursor, and Antigravity out of the box.

    Args:
        arch_data:  Parsed ARCHITECTURE.yaml dict.
        output_dir: Project root directory (AGENTS.md written here).

    Returns:
        Path to the written AGENTS.md.
    """
    system = arch_data.get("system", {})
    workers = arch_data.get("workers", [])
    system_name = system.get("name", "FORGE SYSTEM")
    outcome = system.get("outcome", "See OUTCOME.md for the full outcome statement.")

    # Worker fleet table rows
    fleet_rows: list[str] = []
    stormbreaker_rows: list[str] = []
    for w in workers:
        name = w.get("name", "WORKER")
        role = w.get("role", "")
        model = w.get("model", "claude-sonnet-4-6")
        risk = _infer_risk_tier(w)
        fleet_rows.append(f"| {name} | {role} | {model} | {risk} |")
        stormbreaker_rows.append(f"- **{name}**: risk tier `{risk}`")

    fleet_table = "\n".join(fleet_rows) if fleet_rows else "| (none) | | | |"
    stormbreaker_list = "\n".join(stormbreaker_rows) if stormbreaker_rows else "- (none)"

    content = textwrap.dedent(f"""\
        # AGENTS.md — {system_name}

        ## What this agent does

        {outcome}

        ## Global command

        `/forge:run` in any Claude Code session from this directory

        ## Worker fleet

        | Worker | Role | Model | Risk tier |
        | ------ | ---- | ----- | --------- |
        {fleet_table}

        ## Runtime adapters

        | Runtime | Command |
        | ------- | ------- |
        | Claude Code | `/forge:run` |
        | Codex | `/prompts:forge-run` |
        | Gemini CLI | `/hephaestus`, then `/forge:run` |
        | Antigravity | `/forge:run` |
        | Terminal | `python3 ~/projects/forge/engine/spawner.py` |

        ## Memory governance

        - Durable memory requires Curator approval (see `forge_state.db` → `memory_candidates` table)
        - Workers write memory candidates, not durable facts
        - Sam approves promotion via `/forge:approve`

        ## Safety boundary

        - No secrets in outputs
        - No autonomous external sends
        - All Gate 2 outputs scanned by `engine/safety_check.py` before release

        ## Stormbreaker risk tiers

        {stormbreaker_list}
    """)

    agents_path = output_dir / "AGENTS.md"
    agents_path.write_text(content)
    return agents_path


def generate_ontology_config(project_dir: Path, arch_data: dict) -> None:
    """
    Write the Hephaestus ontology configuration files to project_dir.

    Creates:
      .agentlas/ontology-sources.json  — tells the ontology runtime what to index
      .agentlas/ontology-inbox/        — drop zone for Sam to add source documents

    Worker-specific source entries are inferred from common FORGE data paths and
    the worker names declared in ARCHITECTURE.yaml.

    Args:
        project_dir: Absolute path to the FORGE project root.
        arch_data:   Parsed ARCHITECTURE.yaml dict.
    """
    agentlas_dir = project_dir / ".agentlas"
    agentlas_dir.mkdir(parents=True, exist_ok=True)

    inbox_dir = agentlas_dir / "ontology-inbox"
    inbox_dir.mkdir(exist_ok=True)

    # Base sources always present
    sources: list[dict] = [
        {"path": "OUTCOME.md", "scope": "internal", "agent": "all"},
        {"path": "ARCHITECTURE.yaml", "scope": "internal", "agent": "all"},
        {"path": "data/knowledge/", "scope": "internal", "agent": "all"},
    ]

    # Per-worker sources inferred from conventional FORGE data paths
    workers = arch_data.get("workers", [])
    worker_source_map: dict[str, str] = {
        "SCOUT":    "data/accounts.csv",
        "ENRICHER": "data/accounts.csv",
        "BRIEFER":  "data/knowledge/",
        "RESEARCHER": "data/knowledge/",
        "WRITER":   "data/templates/",
        "REVIEWER": "data/outputs/",
        "CURATOR":  "data/outputs/",
    }
    seen_paths: set[str] = {s["path"] for s in sources}
    for w in workers:
        name = str(w.get("name", "")).upper()
        path = worker_source_map.get(name)
        if path and path not in seen_paths:
            sources.append({"path": path, "scope": "internal", "agent": name})
            seen_paths.add(path)

    config = {
        "sources": sources,
        "ontology_runtime": "~/.agentlas/runtime/current/bin/ontology",
        "query_hook": "bin/ontology query \"{query}\" --agent {worker_name}",
    }

    sources_path = agentlas_dir / "ontology-sources.json"
    sources_path.write_text(json.dumps(config, indent=2))
    print(f"[FORGE] Ontology config written: .agentlas/ontology-sources.json")
    print(f"[FORGE] Ontology inbox ready:    .agentlas/ontology-inbox/ (drop source docs here)")


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

    # --- Hephaestus: AGENTS.md ---
    agents_path = generate_agents_md(arch_data, output_dir)
    print(f"[FORGE] AGENTS.md written to {agents_path.resolve()}")

    # --- Hephaestus: ontology config + inbox ---
    generate_ontology_config(output_dir, arch_data)

    # Generate individual worker contracts (with routing_card section)
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
    print(f"[FORGE] Hephaestus artefacts: AGENTS.md + .agentlas/ written.")
    print(f"[FORGE] Next step: python3 ~/projects/forge/engine/spawner.py")


if __name__ == "__main__":
    main()

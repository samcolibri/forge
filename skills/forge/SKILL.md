---
name: forge
description: >-
  Outcome-based autonomous agent OS. Define any outcome — FORGE designs the worker fleet,
  runs the loop until done, QA-gates every output, and reports hourly. Type /forge:run
  to start a new outcome, /forge:design to architect without running, /forge:status to
  check active loops, /forge:approve to sign off and start execution.
version: "1.0"
author: samcolibri
---

# FORGE — Outcome Operating System

FORGE is not a project template. It is not a scaffold. It is an outcome-execution engine.

You define what done looks like. FORGE designs the worker fleet to get there. It runs the loop autonomously until the binary outcome question resolves to YES, a human stops it, or the iteration ceiling is hit. Every output is QA-scored before it advances. Anything that leaves the system requires a human gate. Sam gets hourly email updates at sam.chaudhary@alliedschools.com regardless of whether anything changed.

The first live system running on FORGE is `moreland-sdr-agent` — 100 school districts, 5 booked meetings, autonomous SDR loop, worker fleet of SCOUT/ENRICHER/VALIDATOR/WRITER/QA_SCORER/REPORTER.

---

## What FORGE Does

### The Core Abstraction

Every FORGE project has three layers:

**1. The Outcome Statement**
A plain-language description of what done looks like. "Book 5 meetings for Robby." "Generate 200 qualified leads." "Migrate all legacy API calls to v3." "Publish 10 research reports with citations." FORGE converts this into a structured fable prompt — the identity document every worker agent reads before executing.

**2. The Architecture**
A generated ARCHITECTURE.yaml that specifies: the worker fleet (named agents with roles), the loop mode (continuous / batch / scheduled), stop conditions, human gates, failure handling, state storage, output schema, and the binary outcome question that determines success.

**3. The Loop**
A Python spawner (`engine/spawner.py`) that reads the architecture and runs the pipeline continuously until a stop condition is met. Workers are dispatched via Claude API (for reasoning/writing), Codex bash subprocess (for deterministic transforms), AGY bash subprocess (for web/research tasks), or any MCP tool.

---

## The Fable Injection

When you run `/forge:run` or `/forge:design`, FORGE calls `engine/inject.py` with your outcome statement. inject.py:

1. Loads `prompts/BASE_FABLE.md` — the master template
2. Fills all nine template variables from your outcome context
3. Writes the completed soul document to `OUTCOME.md` in the project directory
4. Passes it to `engine/architect.py` to design the worker fleet

Template variables injected into BASE_FABLE.md:
- `SYSTEM_NAME` — agent name derived from outcome (e.g., ROBBY, ATLAS, SCOUT)
- `OUTCOME_STATEMENT` — verbatim outcome as stated by Sam
- `BINARY_CRITERION` — the YES/NO question that ends the loop
- `WORKER_FLEET_LIST` — comma-separated agent names and roles
- `LOOP_STOP_CONDITION` — condition expression for the loop to halt
- `HUMAN_GATE_1` — description of the architecture approval gate
- `HUMAN_GATE_2` — description of the output release gate
- `REPORT_RECIPIENT` — email for hourly reports (default: sam.chaudhary@alliedschools.com)
- `PROJECT_REPO` — GitHub repo slug (e.g., samcolibri/moreland-sdr-agent)

Every worker agent receives the full OUTCOME.md before executing. The soul document is not a README — it is the identity. It tells each worker why it exists, what law it operates under, and what the binary question is. An agent that has not read the soul document cannot work on FORGE.

---

## The 4 Commands

### `/forge:run "<outcome>"`
Start a new outcome loop end-to-end. Injects the fable, designs the architecture, presents it to Sam for sign-off, then starts the loop in background on approval.

**Use when:** You want to launch a new autonomous project from scratch.

### `/forge:design "<outcome>"`
Architecture only — no execution. Inject the fable, generate ARCHITECTURE.yaml, show Sam the worker fleet design, wait for feedback. Loop does not start.

**Use when:** You want to see the design before committing. Or when the project needs stakeholder sign-off before any compute runs.

### `/forge:status`
Check all active loops: outcome progress, QA scores, pending human gates, last hourly report, iteration counts. Reads from `~/projects/forge/state/<project>.db`.

**Use when:** You want to know where any running project stands without touching the loop.

### `/forge:approve`
Human sign-off gate. Reads all pending items (sequences, CSVs, outputs) waiting for Sam's approval. Shows each item, lets Sam approve/reject, releases approved items back into the loop.

**Use when:** REPORTER has flagged outputs awaiting human review, or the architecture gate needs sign-off to start execution.

---

## Human Gates

FORGE has two mandatory human gates and one optional phase gate:

**Gate 1 — Architecture Sign-Off (mandatory, before loop starts)**
Sam reviews ARCHITECTURE.yaml. Workers, stop conditions, output schema, human review points are all visible. Sam explicitly approves or requests changes. The loop cannot start until Gate 1 is cleared.

**Gate 2 — Output Release (mandatory, before anything leaves system)**
No sequence, CSV, email, API write, or external action happens without Sam (or the designated reviewer) approving it. REPORTER surfaces pending outputs. Sam reviews and approves/rejects each batch. Rejections go back to the WRITER with feedback.

**Phase Gates (optional, configured per project)**
Multi-phase projects can require explicit approval to advance from one phase to the next (e.g., "pilot complete, advance to full rollout?"). Configured in ARCHITECTURE.yaml under `loop.phase_gates`.

These gates cannot be bypassed, automated away, or skipped under time pressure. If a gate is not cleared within 48 hours, REPORTER escalates to sam.chaudhary@alliedschools.com with a summary of what is blocked.

---

## Cross-Repo Operation

FORGE is self-contained. It runs in any directory. When you type `/forge:run` in the `moreland-sdr-agent` directory or the `atlas-pipeline-agent` directory or a brand-new blank folder, FORGE operates the same way:

1. All engine scripts live at `~/projects/forge/engine/`
2. The base fable lives at `~/projects/forge/prompts/BASE_FABLE.md`
3. ARCHITECTURE.yaml is written to the **current project directory**
4. OUTCOME.md is written to the **current project directory**
5. State DB is written to `<current_project>/data/state.db`
6. The global state registry lives at `~/projects/forge/state/` (one entry per active project)

This means: one FORGE installation, unlimited projects. Each project gets its own soul document, its own architecture, its own state. FORGE reads the current directory and knows which project it is serving.

---

## The Worker Model

Workers are named agents assigned to pipeline stages in ARCHITECTURE.yaml. FORGE supports four worker types:

**Claude API workers** — for reasoning, writing, scoring, synthesis. Called via the `anthropic` Python SDK. Prompt is soul document fragment plus task payload. Model selection: Opus for QA scoring, Sonnet for writing, Haiku for classification and routing.

**Codex workers (bash subprocess)** — for deterministic transforms: CSV parsing, deduplication, schema validation, file writes. Called via `subprocess.run(["codex", ...])`.

**AGY workers (bash subprocess)** — for web research, URL resolution, public data retrieval. Called via `subprocess.run(["agy", ...])`.

**MCP tool workers** — for any connected MCP server: Salesforce reads, HubSpot reads, Airtable writes, Slack notifications. Called via the MCP tool interface available in the Claude Code session where FORGE is running.

Worker definitions in ARCHITECTURE.yaml include: `name`, `type` (claude/codex/agy/mcp), `model` (for claude type), `role`, `input_schema`, `output_schema`, `retry_max`, `failure_action`.

---

## Quality Bar

Every FORGE output passes through a QA_SCORER worker before it is visible to any human or considered for release. The QA bar is non-negotiable:

- **Minimum score: 8.5/10** against the project's scoring rubric (defined in ARCHITECTURE.yaml under `qa.rubric`)
- **On fail:** WRITER retries with scorer feedback. Maximum 2 retries.
- **On triple fail:** Output is flagged `QA_ESCALATION`, REPORTER emails Sam, item is held until human intervenes.
- **Score logged:** Every QA score is written to the state DB with timestamp, worker, rubric breakdown, and retry count.

The rubric is project-specific but always includes: accuracy (no invented facts), tone (matches brand voice), specificity (named pain, not generic claim), structure (correct format), and action clarity (single clear CTA).

---

## Loop Mechanics

The loop runs until one of three stop conditions:

1. **`outcome_met = true`** — the binary criterion resolves YES (e.g., "5 booked meetings confirmed")
2. **`human_stop`** — Sam explicitly halts via `/forge:status` or by setting `state.stop_requested = true` in the state DB
3. **`max_iterations`** — the configured ceiling is hit (prevents runaway loops on bad data)

Between iterations:
- State DB is updated after every worker completes
- REPORTER runs every 3600 seconds regardless of pipeline state
- Pending human gate items accumulate in the `approvals` table — they do not block the loop (next accounts proceed while pending items await review)
- Failures are logged, flagged, and skipped — the queue always keeps moving

---

## Reporting

**Hourly email** to sam.chaudhary@alliedschools.com via SMTP (configured in `~/.forge.env`)

Email always contains:
- Project name + current outcome statement
- Binary question + current status (YES / NO / IN_PROGRESS)
- Accounts/items processed this hour vs. total
- QA scores: mean, min, escalations
- Pending approvals: count + brief description
- Errors or skips since last report
- Estimated time to completion (if calculable)

**Live dashboard** at samcolibri.github.io/forge (GitHub Pages, auto-updated by REPORTER after each hourly email). Shows all active projects, progress bars, last QA score, pending gates.

---

## Active Projects on FORGE

| Project | Outcome | Status |
|---|---|---|
| `moreland-sdr-agent` | 5 booked meetings from 100 accounts | awaiting_gate_1_approval |

---

## File Layout

```
~/projects/forge/
  skills/forge/
    SKILL.md                  <- this file
    commands/
      run.md                  <- /forge:run command
      design.md               <- /forge:design command
      status.md               <- /forge:status command
      approve.md              <- /forge:approve command
  prompts/
    BASE_FABLE.md             <- master fable template
  engine/
    inject.py                 <- fills BASE_FABLE.md with outcome vars
    architect.py              <- designs ARCHITECTURE.yaml from fable
    spawner.py                <- starts loop in background
    qa_scorer.py              <- shared QA scoring logic
    reporter.py               <- hourly email + dashboard update
  state/
    registry.json             <- global index of all active projects
    <project_id>.db           <- per-project state mirror (read-only copy)
```

Per-project files (written to the project directory, not ~/projects/forge/):
```
<project>/
  OUTCOME.md                  <- soul document (filled fable)
  ARCHITECTURE.yaml           <- worker fleet + loop config
  agent.plan.json             <- harness-next task plan (required)
  data/
    state.db                  <- live state (sqlite)
    accounts.csv              <- input queue
    output/                   <- approved outputs
    logs/                     <- per-worker logs
```

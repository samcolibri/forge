# FORGE × Hephaestus

> Define the outcome. FORGE runs the loop. Hephaestus governs every step.

**Not a project tool. An outcome execution OS with 99.26% operational robustness.**

[Live Dashboard →](https://samcolibri.github.io/forge/) | [First Project: Moreland SDR →](https://samcolibri.github.io/moreland-sdr-agent/)

---

## What it does

You type `/forge:run "book 5 meetings for Moreland school districts"`.

FORGE:
1. Converts your outcome into a structured fable prompt (CL4R1T4S injection style)
2. Designs a worker fleet with Stormbreaker-governed routing cards (SCOUT → ENRICHER → WRITER → QA → REPORTER)
3. Generates `AGENTS.md` for multi-runtime deployment (Claude Code / Codex / Gemini / Cursor / Antigravity)
4. Shows you the architecture — you sign off
5. Runs the 8-gate Stormbreaker protocol autonomously until the outcome is met
6. Public safety scan runs before Gate 2 — nothing leaves without sign-off
7. Hourly email + live dashboard so you never have to ask "what is happening?"

---

## Why Stormbreaker matters

Stormbreaker is a protocol from Hephaestus that puts every worker output through 8 mandatory verification gates. In benchmarks it achieves **99.26% operational robustness vs. 76.48% for native agent execution** — a 23-point gap that eliminates the class of silent failures that make autonomous agents unreliable in production.

---

## Architecture

```
OUTCOME (one sentence)
    ↓ inject.py → BASE_FABLE.md (CL4R1T4S soul doc)
    ↓ architect.py → ARCHITECTURE.yaml + AGENTS.md + worker routing cards
    ↓ [Sam signs off]
    ↓ spawner.py → loop.py (24/7 daemon)
         ↓
    STORMBREAKER GATE (per worker)
    scope_lock → contract → failure_memory → verify_plan
    → evidence_loop → review_gate → outcome_ledger → final_gate
         ↓
    ONTOLOGY BRIDGE (local-first, Hephaestus runtime)
    project docs indexed → SCOUT queries locally before Exa
         ↓
    WORKER POOL (Claude API / Codex / AGY / MCP)
         ↓
    PUBLIC SAFETY SCAN (before Gate 2)
         ↓
    HUMAN GATE 2 (Sam approves, nothing leaves without sign-off)
         ↓
    OUTCOME VALIDATOR (met? done : loop again)
         ↓
    REPORTER (hourly email + GitHub Pages)
```

---

## Install

```bash
# Install Hephaestus runtimes
curl -fsSL https://raw.githubusercontent.com/agentlas-ai/Hephaestus/v0.7.0/scripts/install-all-runtimes.sh | bash

# Link FORGE as a Claude Code skill
ln -sf ~/projects/forge/skills/forge ~/.claude/skills/forge
```

Then in any Claude Code session: `/forge:run`

---

## Use in any repo

```bash
/forge:run "book 5 meetings for Moreland school districts"
/forge:design "build a renewal alert system"
/forge:status
/forge:approve
```

---

## Projects running on FORGE

| Project | Outcome | Stormbreaker | Status |
|---------|---------|--------------|--------|
| moreland-sdr | 5 booked meetings / 100 accounts | active | awaiting API keys |
| simplenursing-nova (demo) | 500K TikTok views | active | demo only |

---

## Stormbreaker gates (Hephaestus)

Every worker output passes through 8 sequential gates before it can advance:

| Gate | Name | What it checks |
|------|------|----------------|
| 1 | `scope_lock` | Output stays within the defined outcome boundary |
| 2 | `issue_contract` | Worker fulfilled its stated contract for this task unit |
| 3 | `failure_memory` | No repeat of a previously failed pattern |
| 4 | `verifier_first_plan` | Independent verifier validates the execution plan |
| 5 | `evidence_loop` | Claims backed by traceable evidence |
| 6 | `review_gate` | QA score 8.5/10 or higher |
| 7 | `outcome_ledger` | Progress logged to immutable ledger |
| 8 | `final_gate` | Human approval required before output leaves the system |

Low-risk tasks may skip to `final_gate`. All others run the full chain.

---

## Worker types

| Worker | Model | Role |
|--------|-------|------|
| SCOUT | Claude Sonnet 4.6 | Research, target discovery, signal surfacing. Queries local ontology first. |
| ENRICHER | Claude Haiku / Codex | Data fill-in, API calls, firmographics |
| VALIDATOR | Claude Haiku | Quality check, dedup, fact verification |
| WRITER | Claude Sonnet 4.6 | Drafts emails, summaries, proposals |
| QA_SCORER | Claude Opus 4.7 | Scores every output 8.5/10 or higher |
| REPORTER | Claude Haiku | Hourly emails + dashboard updates |

---

## Stack

| Component | File | Description |
|-----------|------|-------------|
| Fable injection | `engine/inject.py` | Outcome → CL4R1T4S soul doc |
| Architect | `engine/architect.py` | Fable → ARCHITECTURE.yaml + AGENTS.md |
| Loop daemon | `engine/loop.py` | 24/7 asyncio orchestrator with Stormbreaker wired in |
| Spawner | `engine/spawner.py` | Launches loop as background process |
| Safety check | `engine/safety_check.py` | Public safety scan before Gate 2 |
| State | `state/` | SQLite (survives restarts) |
| Dashboard | `docs/index.html` | GitHub Pages, auto-updated hourly |

| Hephaestus component | Role |
|----------------------|------|
| Stormbreaker protocol | 8-gate governance per worker output |
| Local ontology runtime (`~/.agentlas/runtime/`) | SCOUT queries project docs before external search |
| AGENTS.md generation | Multi-runtime routing cards for every worker |

---

## State machine

Every task unit moves through:

```
queued → dispatching → executing → stormbreaker_gates (1-8)
  ↓ (gate fail: retry)                ↓ (all gates pass)
                              pending_approval
                              ↓ approved  ↑ rejected
                              exported → outcome_check
                              ↓ not met: loop again
                              done
```

---

## CLI commands

```bash
# Start a new project
/forge:run "your outcome in plain English"

# Design architecture without starting loop
/forge:design "your outcome"

# Check loop status
/forge:status

# Review and approve pending outputs
/forge:approve --project <name>

# Pause the loop (resumable)
/forge:stop --project <name>
/forge:run --resume --project <name>
```

---

## Repo layout

```
forge/
  engine/          core loop, dispatcher, state machine, safety_check
  prompts/         BASE_FABLE and injection templates
  skills/forge     Claude Code skill (symlink target)
  state/           SQLite db (gitignored)
  docs/            GitHub Pages dashboard
  FORGE.md         Identity document — the soul of FORGE
```

---

*Built by Sam · Powered by Claude + Hephaestus · 2026*

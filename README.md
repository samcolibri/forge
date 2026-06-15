# FORGE

> Define the outcome. FORGE runs the loop.

**Not a project tool. An operating system for outcome-based agents.**

[Live Dashboard →](https://samcolibri.github.io/forge/) | [First Project: Moreland SDR →](https://samcolibri.github.io/moreland-sdr-agent/)

---

## What it does

You type `/forge:run "book 5 meetings for Moreland school districts"`.

FORGE:
1. Converts your outcome into a structured fable prompt (CL4R1T4S injection style)
2. Designs a worker fleet specific to your outcome (SCOUT → ENRICHER → WRITER → QA → REPORTER)
3. Shows you the architecture — you sign off
4. Runs the loop autonomously until the outcome is met
5. Human gates control anything that leaves the system
6. Hourly email + live dashboard so you never have to ask "what's happening?"

---

## Install as a Claude Code skill

```bash
# One command — works in any repo after this
forge_path=~/projects/forge
ln -s "$forge_path/skills/forge" ~/.claude/skills/forge
```

Then in any Claude Code session: `/forge:run`

---

## Projects running on FORGE

| Project | Outcome | Status |
|---------|---------|--------|
| [moreland-sdr](../moreland-sdr-agent) | ≥5 booked meetings from 100 school districts | Awaiting API keys |

---

## Architecture

```
OUTCOME INPUT (any format: text / paste / notes / PDF)
  → INJECT       BASE_FABLE → structured fable prompt via Claude
  → ARCHITECT    fable → worker fleet + ARCHITECTURE.yaml
  → [Human signs off]
  → SPAWNER      starts loop as background daemon
  → LOOP ORCHESTRATOR (24/7)
      → WORKER POOL       Claude API / Codex bash / AGY / any tool
      → QA GATE           score ≥ 8.5 / 10 to pass
      → [Human approves outputs]
      → OUTCOME VALIDATOR met? → done  else → loop again
  → REPORTER     hourly email + GitHub Pages update
```

See the [live dashboard](https://samcolibri.github.io/forge/) for the interactive diagram.

---

## Worker archetypes

| Worker | Model | Role |
|--------|-------|------|
| SCOUT | Claude Sonnet 4.6 | Research, target discovery, signal surfacing |
| ENRICHER | Claude Haiku / Codex | Data fill-in, API calls, firmographics |
| VALIDATOR | Claude Haiku | Quality check, dedup, fact verification |
| WRITER | Claude Sonnet 4.6 | Drafts emails, summaries, proposals |
| QA_SCORER | Claude Opus 4.7 | Scores every output ≥ 8.5/10 |
| REPORTER | Claude Haiku | Hourly emails + dashboard updates |

---

## State machine

Every task unit moves through:

```
queued → dispatching → executing → qa_scoring
  ↓ (score < 8.5: retry)         ↓ (score ≥ 8.5)
                          pending_approval
                          ↓ approved  ↑ rejected
                          exported → outcome_check
                          ↓ not met: loop again
                          done ✓
```

---

## CLI commands

```bash
# Start a new project
/forge:run "your outcome in plain English"

# Check loop status
/forge:status

# Review and approve pending outputs
/forge:approve --project <name>

# Pause the loop (resumable)
/forge:stop --project <name>
/forge:run --resume --project <name>
```

---

## Stack

- **Outcome injection:** Claude Sonnet 4.6 (fable generation)
- **Worker execution:** Claude (API) · Codex (bash) · AGY (bash) · any tool
- **QA:** Claude Opus 4.7 (scores every output ≥ 8.5/10)
- **State:** SQLite (survives restarts)
- **Reporting:** SMTP email + GitHub Pages (hourly)
- **Loop:** Python asyncio daemon

---

## Repo layout

```
forge/
  engine/          core loop, dispatcher, state machine
  prompts/         BASE_FABLE and injection templates
  skills/forge     Claude Code skill (symlink target)
  state/           SQLite db (gitignored)
  docs/            GitHub Pages dashboard
```

---

*Built by Sam · Powered by Claude · 2026*

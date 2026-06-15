# /forge:design — Architect Without Executing

**Command:** `/forge:design "<outcome statement>"`

Run this command when Sam wants to see the worker fleet designed and sign off on the architecture before anything runs. The loop does NOT start. No background process is spawned. No emails are sent.

Use this when:
- Sam wants to review the architecture with a stakeholder before committing compute
- A project needs a design document before leadership approval
- Sam wants to iterate on the architecture several times before running
- The outcome is complex enough to warrant a review session

---

## What This Command Does

Steps 1–4 are identical to `/forge:run`. Steps 5–6 are different.

---

## Step 1 — Read the Outcome

If Sam provided the outcome as an argument, use it verbatim.

If Sam typed just `/forge:design` with no argument, ask:

> What outcome do you want to design a worker fleet for? Describe what "done" looks like.

---

## Step 2 — Inject the Fable

```bash
python3 ~/projects/forge/engine/inject.py "<outcome>"
```

Writes `OUTCOME.md` to the current directory. If inject.py fails, stop and show the error.

---

## Step 3 — Architect the Worker Fleet

```bash
python3 ~/projects/forge/engine/architect.py
```

Reads `OUTCOME.md`, generates `ARCHITECTURE.yaml` in the current directory. If architect.py fails, stop and show the error.

---

## Step 4 — Present the Architecture

Read `ARCHITECTURE.yaml` and summarize:

```
FORGE Architecture — <SYSTEM_NAME>
Outcome: <OUTCOME_STATEMENT>
Binary question: <BINARY_CRITERION>

Worker Fleet:
  [1] <WORKER_NAME> (<type>) — <role>
  [2] <WORKER_NAME> (<type>) — <role>
  ...

Loop: <loop.mode> | batch size: <batch_size> | max iterations: <max_iterations>

Stop conditions:
  - <condition_1>
  - <condition_2>

Human Gates:
  Gate 1 (Architecture): Sam approves this design before loop starts
  Gate 2 (Output): <reviewer> approves all outputs before release

QA Rubric: <rubric_summary>
```

---

## Step 5 — Collect Feedback (Design Mode Gate)

In design mode, do NOT ask for a yes/no start decision. Instead ask:

> **Architecture review:**
> - Type **looks good** to lock this design (no loop starts yet)
> - Type **changes: [what to change]** to iterate
> - Type **run it** to approve AND immediately start the loop (same as /forge:run)

**If Sam says "looks good":** confirm the design is saved. Output:
```
Architecture locked for <SYSTEM_NAME>.

Files saved:
  OUTCOME.md         — soul document
  ARCHITECTURE.yaml  — worker fleet design

When ready to run: /forge:run (will skip re-design, detect existing ARCHITECTURE.yaml)
```

**If Sam requests changes:** update ARCHITECTURE.yaml or re-run architect.py with modified OUTCOME.md, re-present. Repeat.

**If Sam says "run it":** proceed exactly as `/forge:run` Step 6 (start the spawner).

---

## Step 6 — Save the Architecture

Whether Sam approves or requests changes, ensure both files are saved to the current directory:

- `OUTCOME.md` — the filled fable (soul document)
- `ARCHITECTURE.yaml` — the full worker fleet design

These files can be committed to the project repo, shared with stakeholders, or used as input for `/forge:run` at a later time.

---

## Sharing the Architecture

After design mode completes, offer to format the architecture for sharing:

> Want me to format this as a shareable brief? I can output:
> - A Slack message summary (< 200 words)
> - A markdown document for the project repo
> - A YAML spec for another engineer to review

Only do this if Sam asks or if the project has a named stakeholder in ARCHITECTURE.yaml.

---

## Notes

- `/forge:design` is safe to run multiple times. It will overwrite `OUTCOME.md` and `ARCHITECTURE.yaml` each time.
- If a loop is already running (check `data/loop.pid`), warn Sam. Design mode does not affect running loops.
- The architecture is not registered in `~/projects/forge/state/registry.json` until the loop actually starts.

# /forge:run — Launch an Outcome Loop

**Command:** `/forge:run "<outcome statement>"`

Run this command when Sam is ready to define a new outcome and start the autonomous loop immediately after architecture sign-off.

---

## What This Command Does

When Sam types `/forge:run "book 5 meetings for Moreland"` (or just `/forge:run` and Claude asks for the outcome), execute these steps in order:

---

## Step 1 — Read the Outcome

If Sam provided the outcome as an argument, use it verbatim.

If Sam typed just `/forge:run` with no argument, ask:

> What is the outcome you want FORGE to pursue? Describe it in plain language — what does "done" look like?

Wait for Sam's answer. Do not proceed until you have a clear outcome statement.

---

## Step 2 — Inject the Fable

Run the fable injection engine:

```bash
python3 ~/projects/forge/engine/inject.py "<outcome>"
```

This command:
- Loads `~/projects/forge/prompts/BASE_FABLE.md`
- Derives SYSTEM_NAME, BINARY_CRITERION, WORKER_FLEET_LIST, and other template vars from the outcome
- Writes the completed soul document to `OUTCOME.md` in the current working directory
- Outputs a JSON summary of the injected variables

If inject.py fails, show the error and stop. Do not continue with a broken fable.

---

## Step 3 — Architect the Worker Fleet

Run the architect:

```bash
python3 ~/projects/forge/engine/architect.py
```

This command reads `OUTCOME.md` (just written) and generates `ARCHITECTURE.yaml` in the current directory. The architect designs:
- Named worker agents with roles (SCOUT, WRITER, QA_SCORER, etc.)
- Pipeline order and dependencies
- Stop conditions with exact criteria
- Human gate definitions (Gate 1 and Gate 2)
- Failure handling per worker
- State DB schema
- Output format spec
- QA rubric with 5–7 scoring dimensions

If architect.py fails, show the error and stop. Do not present a partial architecture.

---

## Step 4 — Present the Architecture to Sam

Read `ARCHITECTURE.yaml` and summarize it clearly. Show:

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

Estimated runtime: <estimate if calculable>
```

Keep the summary scannable. The full ARCHITECTURE.yaml is on disk if Sam wants details.

---

## Step 5 — Gate 1: Architecture Sign-Off

Ask Sam explicitly:

> **Sign off on this architecture to start the loop?**
> - Type **yes** to approve and start
> - Type **changes: [what to change]** to request modifications
> - Type **no** to cancel

**If Sam approves:** proceed to Step 6.

**If Sam requests changes:** update ARCHITECTURE.yaml manually or re-run architect.py with the modified OUTCOME.md, then re-present. Repeat until Sam approves.

**If Sam cancels:** confirm cancellation. OUTCOME.md and ARCHITECTURE.yaml remain on disk for future reference. No loop starts.

Do not start the loop without explicit approval. Gate 1 cannot be skipped.

---

## Step 6 — Start the Loop

On Sam's approval, register the project in the FORGE state registry and start the spawner:

```bash
python3 ~/projects/forge/engine/spawner.py --project "$(pwd)" --background
```

The spawner:
- Creates `data/state.db` with initial schema
- Seeds the input queue from `data/accounts.csv` (or equivalent input file)
- Starts the pipeline loop as a background process
- Writes the PID to `data/loop.pid`
- Sends an immediate "loop started" email to sam.chaudhary@alliedschools.com
- Registers the project in `~/projects/forge/state/registry.json`

---

## Step 7 — Confirm Launch

Output a clean confirmation to Sam:

```
Loop started for <SYSTEM_NAME>.

Outcome: <OUTCOME_STATEMENT>
Binary question: <BINARY_CRITERION>

The loop is running in background (PID: <pid>).
First hourly report: sam.chaudhary@alliedschools.com in ~60 minutes.
Live status: samcolibri.github.io/forge

To check progress: /forge:status
To review pending outputs: /forge:approve
To stop the loop: set state.stop_requested = true in data/state.db
```

---

## Error Handling

**inject.py fails:** Show the error. Check that BASE_FABLE.md exists at `~/projects/forge/prompts/BASE_FABLE.md`. Do not continue.

**architect.py fails:** Show the error. Check that OUTCOME.md was written correctly. Do not continue.

**spawner.py fails:** Show the error. The architecture files remain intact — the loop can be started manually once the error is resolved.

**No accounts.csv:** Warn Sam that the input queue is empty. Offer to help create it before starting the loop.

---

## Notes

- This command works in any directory. FORGE is not tied to the forge project dir.
- The current working directory becomes the project root. All output files go there.
- If OUTCOME.md or ARCHITECTURE.yaml already exist in the current directory, warn Sam before overwriting.
- If a loop is already running for this directory (check `data/loop.pid`), warn Sam and ask if they want to restart.

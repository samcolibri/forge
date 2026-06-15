# /forge:status — Check Active Loops

**Command:** `/forge:status`
**Aliases:** `/forge:status <project>` to check a specific project

Run this command to see the live state of all FORGE loops without touching any of them.

---

## What This Command Does

Reads state from the FORGE registry and all active project state DBs. Displays a dashboard of every running loop.

---

## Step 1 — Read the Registry

```bash
cat ~/projects/forge/state/registry.json
```

This file lists every project that has ever started a FORGE loop, with fields: `project_id`, `project_path`, `outcome`, `status`, `started_at`, `last_updated`.

If the registry is empty or missing, output:
```
No active FORGE loops. Start one with /forge:run.
```

---

## Step 2 — Read Each Project's State DB

For each active project in the registry, query its `data/state.db`:

```bash
python3 ~/projects/forge/engine/state_reader.py --project "<project_path>"
```

This outputs a JSON summary with:
- `outcome` — the original outcome statement
- `binary_question` — the YES/NO criterion
- `outcome_met` — true/false/in_progress
- `accounts_total` — total input items
- `accounts_processed` — items processed so far
- `accounts_pending` — items remaining in queue
- `meetings_booked` or equivalent outcome metric
- `qa_scores` — mean, min, max across all scored outputs
- `qa_escalations` — count of QA failures needing human review
- `pending_approvals` — items waiting for Gate 2 sign-off
- `last_report_sent` — timestamp of last hourly email
- `loop_pid` — current spawner PID (or "stopped" if not running)
- `iterations` — current iteration count vs. max
- `errors_since_last_report` — count of logged errors
- `last_error` — most recent error message

---

## Step 3 — Display the Status Dashboard

Format the output cleanly:

```
FORGE Status — <current datetime>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ACTIVE] <SYSTEM_NAME>
Project: <project_path>
Outcome: <outcome_statement>
Binary:  <binary_question>
Status:  <outcome_met>

Progress:  <accounts_processed>/<accounts_total> items processed
Outcome metric: <metric_name>: <metric_value> / <target>
QA scores: avg <qa_mean>/10 | min <qa_min>/10 | escalations: <qa_escalations>

Pending approvals: <pending_approvals> items awaiting Gate 2 sign-off
Last report sent: <last_report_sent>
Loop PID: <loop_pid>
Iterations: <iterations>/<max_iterations>
Errors (since last report): <errors_since_last_report>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[COMPLETED] <SYSTEM_NAME>
...

[STOPPED] <SYSTEM_NAME>
...
```

If there are pending approvals in any project, add a call to action:
```
ACTION NEEDED: <count> output(s) pending your approval.
Run /forge:approve to review.
```

---

## Specific Project Status

If Sam runs `/forge:status moreland-sdr-agent` or `/forge:status <path>`:

Only show the status for that project. Match by either project_id, SYSTEM_NAME, or path substring.

If no match is found:
```
No FORGE loop found for "<query>". Active projects: <list>
```

---

## Loop Health Indicators

After displaying the dashboard, add a brief health assessment:

- If loop PID is not running but outcome_met is false: warn that the loop may have stopped unexpectedly. Suggest: `python3 ~/projects/forge/engine/spawner.py --project "<path>" --resume`
- If QA escalations > 3: flag that multiple outputs need human review
- If pending approvals > 10: flag that the approval queue is growing
- If last_report_sent > 90 minutes ago: warn that the reporter may be down
- If errors_since_last_report > 5: flag elevated error rate

---

## Notes

- This command is read-only. It never modifies state, starts processes, or sends emails.
- Works from any directory — reads from `~/projects/forge/state/registry.json`
- If state_reader.py is not found, fall back to reading `data/state.db` directly with sqlite3

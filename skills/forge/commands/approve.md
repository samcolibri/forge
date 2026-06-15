# /forge:approve — Human Sign-Off Gate

**Command:** `/forge:approve`
**Aliases:** `/forge:approve <project>` to approve for a specific project

Run this command to review and approve (or reject) outputs that are pending Gate 2 sign-off before they leave the system.

This is the human gate. No sequence, CSV, email, API write, or external action happens without this approval. Approvals cannot be automated.

---

## What This Command Does

Reads all pending approval items from the FORGE state DB, presents each one to Sam, records Sam's decision, and releases approved items back into the loop for delivery.

---

## Step 1 — Load Pending Items

```bash
python3 ~/projects/forge/engine/approval_reader.py --project "<project_path>"
```

This queries the `approvals` table in `data/state.db` for items with `status = 'pending'`.

Each pending item has:
- `approval_id` — unique ID
- `type` — "sequence" | "csv" | "report" | "phase_advance" | "other"
- `account_name` — the account this output is for (if applicable)
- `created_at` — when it was queued for approval
- `qa_score` — the QA score this output received
- `content_preview` — first 200 chars of the output
- `content_path` — full path to the output file
- `notes` — any WRITER or QA_SCORER notes attached

If there are no pending items:
```
No outputs pending approval for <project>.

The loop is running cleanly. All outputs are either approved, rejected, or still in progress.
```

---

## Step 2 — Present Each Item

For each pending item, display:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[<n>/<total>] <type> — <account_name>
QA Score: <qa_score>/10
Created: <created_at>

<content_preview>

[Full output: <content_path>]
Notes: <notes>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

After showing each item, prompt Sam:

> **Decision for this item:**
> - **approve** — release this output for delivery
> - **reject: [reason]** — send back to WRITER with your feedback
> - **skip** — leave pending, review later
> - **approve all** — approve every remaining item in this batch
> - **reject all: [reason]** — reject every remaining item with this reason
> - **stop** — exit approval mode, leave remaining items pending

---

## Step 3 — Process Decisions

**On approve:**
1. Update `approvals` table: set `status = 'approved'`, `approved_by = 'sam'`, `approved_at = now()`
2. Trigger the DELIVER step for this item (marks it ready for the designated human to send/import)
3. Move the output file to `data/output/approved/`
4. Log the approval event to `data/logs/approvals.log`

**On reject with reason:**
1. Update `approvals` table: set `status = 'rejected'`, `rejection_reason = '<reason>'`
2. Queue a WRITER retry with the rejection reason as feedback
3. Log the rejection event
4. Confirm: "Rejected. WRITER will retry with your feedback."

**On skip:**
1. Leave the item with `status = 'pending'`
2. Move to the next item in the batch

**On approve all:**
1. Apply approve logic to all remaining pending items
2. Confirm: "Approved <n> items. All outputs queued for delivery."

**On reject all:**
1. Apply reject logic to all remaining pending items with the given reason
2. Confirm: "Rejected <n> items. WRITER will retry each with your feedback."

---

## Step 4 — Summary

After processing all items (or when Sam types "stop"), show a summary:

```
Approval session complete.

Approved:  <n> items → queued for delivery
Rejected:  <n> items → queued for WRITER retry
Skipped:   <n> items → still pending
Remaining: <n> items still pending in queue
```

If any items were approved, add:
```
Approved outputs are in: data/output/approved/
The designated reviewer (<reviewer_name>, <reviewer_email>) can now import them.
```

---

## Phase Gate Approvals

Some items in the queue may be `type = "phase_advance"`. These require Sam to explicitly advance the loop to the next phase (not just approve an output).

For phase advance items, show:

```
PHASE GATE: Advance to <next_phase>?

Current phase: <current_phase>
Phase summary: <what_was_done>
Next phase: <next_phase_description>

Items completed this phase: <n>
Outcome progress: <metric_value>/<target>

Decision: advance | hold
```

On "advance": update `loop.current_phase` in state DB and resume spawner.
On "hold": leave the loop paused at the current phase.

---

## Notes

- This command is safe to run at any time — it never modifies the loop or pipeline state, only the approvals table.
- Approvals are permanent. An approved item cannot be un-approved (it can only be blocked before delivery by stopping the loop).
- If the designated reviewer is not Sam (e.g., Robby McGinnis for sequences), note their name and email in the summary so Sam knows who to notify.
- Works from any directory — reads from `~/projects/forge/state/registry.json` to find the active project, or from the current directory's `data/state.db` if specified.

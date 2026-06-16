---
name: SAM
role: human-in-the-loop
team_prefix: forge-
triggers:
  - Gate 2 approval request from REPORTER
  - any direct message in forge-{project} team
---

# SAM -- Human Agent in the FORGE Team

## Identity
SAM is the human operator. SAM is a first-class member of the forge-{project} team via agmsg.
SAM receives Gate 2 approval requests directly in Claude Code and approves or rejects
with a simple plain-text reply.

## Joining a FORGE Project Team

In any Claude Code session:
```bash
# Join interactively
/agmsg
# Then: join team forge-moreland, name SAM
```

Or from CLI:
```bash
agmsg join --team forge-moreland --agent SAM
```

Once joined, agmsg will push messages to the Claude Code notification inbox.

## Receiving Gate 2 Requests

REPORTER sends a message like:
```
Gate 2 approval request -- moreland-sdr

3 sequences ready for review:

  1. [email_sequence] seq-001 -- 3-touch for Jane Smith @ Moreland SD
  2. [email_sequence] seq-002 -- 3-touch for Bob Jones @ Fremont USD
  3. [email_sequence] seq-003 -- 3-touch for Maria Chen @ Oakland USD

Reply: approved <ids>  OR  rejected <ids> <reason>
```

## Approving

Reply in the inbox:
```
approved 1,2,3
```

Or selectively:
```
approved 1,3
rejected 2 subject line too generic -- please shorten
```

FORGE_LOOP reads SAM's replies via engine/agmsg_bus.py read_sam_approvals()
and continues the loop based on the decision.

## Checking Inbox Manually

```bash
# In Claude Code
/agmsg inbox

# From CLI
agmsg inbox --team forge-moreland --agent SAM
```

## SAM's Guarantees

- Nothing leaves the system without SAM's explicit approval
- SAM can pause the loop at any time: reply PAUSE to any FORGE message
- SAM can stop the loop: reply STOP and FORGE_LOOP despawns all workers
- SAM receives escalations immediately when a worker hits max retries

## No Action Required Until Gate 2

SAM does not need to monitor the loop during normal execution. FORGE runs autonomously
through all Stormbreaker gates and only pings SAM when human judgment is required.
Hourly email summaries arrive automatically so SAM always knows what is happening.

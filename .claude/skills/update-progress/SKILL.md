---
name: update-progress
description: Refresh runtime progress files for a long-running Phase 1 task so NanoBot can send heartbeat updates.
---

# Update Progress

Use this skill whenever a long-running task starts, reaches a milestone, gets blocked, or finishes.

## Required Files

- `runtime/ACTIVE_TASK.md`
- `runtime/QQ_PROGRESS.md`

## Rules

Update both files so they stay consistent.

`runtime/ACTIVE_TASK.md` should capture:

- status
- task
- owner
- started time
- last updated time
- next checkpoint

`runtime/QQ_PROGRESS.md` should capture a QQ-friendly summary:

- current status
- latest completed milestone
- next step
- whether the task is still running normally

## Writing Style

- Use short Chinese lines
- Keep the QQ version easy to read on a phone
- If nothing important changed, say so clearly instead of inventing fake progress

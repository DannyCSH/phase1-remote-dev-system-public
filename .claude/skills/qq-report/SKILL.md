---
name: qq-report
description: Rewrite the current result into a concise QQ-friendly Chinese report for remote reading.
---

# QQ Report

Use this skill when a result needs to be sent back through NanoBot to QQ.

## Output Rules

- Write in Chinese
- First sentence: the headline result
- Then give a short status summary
- Then mention risk or next step only if needed
- Keep the main body readable on a phone
- Do not dump raw logs unless the user explicitly asks

## If Files Matter

Mention important paths or artifacts clearly, for example:

- changed files
- generated documents
- local web addresses
- output directories

## Runtime Sync

If the task is still ongoing, also update `runtime/QQ_PROGRESS.md` so the next heartbeat stays consistent.

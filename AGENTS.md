# Agent Instructions

This workspace is a project-scoped remote development gateway rooted at the current repository.

Current Phase 1 target:
`QQ -> NanoBot -> Claude Code -> Codex`

## Core Rules

- Default to Simplified Chinese.
- Prefer project-scoped changes. Avoid global changes unless they are truly required.
- Prefer `D:` over `C:` whenever possible.
- Long tasks must stay observable from the phone side.
- Do not fake progress. If nothing important changed, say so plainly.

## Role Split

- `NanoBot`: QQ ingress, lightweight router, status bridge.
- `Claude Code`: main executor for most implementation work.
- `Codex`: high-judgment helper for task understanding, hard parts, review, and rescue.

## Execution Rules

- For development and system tasks, NanoBot should route into the local Phase 1 worker instead of trying to solve the whole task itself.
- Claude should remain the main executor after the task is understood.
- Codex should be used early for task understanding and later for review, but should not immediately take over normal implementation.
- Do not create an endless Claude/Codex loop.

## Runtime Files

- `runtime/ACTIVE_TASK.md`: the current active task state.
- `runtime/QQ_PROGRESS.md`: short phone-friendly progress.
- `runtime/LAST_ARTIFACTS.json`: files or URLs worth sending back to QQ.
- `runtime/queue/`: pending and processing task queue.
- `runtime/sessions/`: Phase 1 session state and per-session jsonl logs.
- `runtime/projects/`: current project state.

## Reporting

- Prefer short, mobile-friendly Chinese.
- Lead with the result, then the current state, then risk or next step if needed.
- If a deliverable is a file, webpage, document, or table, point to its exact path or URL.

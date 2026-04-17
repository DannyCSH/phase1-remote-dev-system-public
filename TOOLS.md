# Tool Usage Notes

Tool signatures are provided automatically by the runtime.
This file adds project-specific expectations.

## Project Paths

- Project root: current repository root
- NanoBot runtime config: `config/nanobot.local.json`
- Main launcher: `scripts/Launch-Phase1Task.ps1`
- Runtime state: `runtime/`

## Preferred Patterns

- Prefer project scripts under `scripts/` over ad-hoc one-off commands.
- Prefer project-local config and rules over global edits.
- For long tasks, keep `runtime/ACTIVE_TASK.md` and `runtime/QQ_PROGRESS.md` current.
- If a task creates a shareable artifact, record it in `runtime/LAST_ARTIFACTS.json`.
- When investigating live state, read `runtime/queue/`, `runtime/sessions/`, and `runtime/projects/` before guessing.

## Environment Notes

- The current stable Phase 1 path is Windows-native.
- WSL + `tmux` remains a future upgrade path, not a hard requirement for this project to run now.
- Avoid destructive system changes unless the user explicitly asks for them.
- If a machine-wide change requires administrator rights or BIOS changes, say that clearly.

# Claude Project Memory

@AGENTS.md

This file is project-scoped and applies only inside this repository.

## Claude-Specific Rules

- Keep this file short and high-signal.
- For non-trivial tasks, decide the verification path before editing files.
- Prefer Claude as the main executor once the task is understood.
- Use Codex for task decomposition, difficult debugging, and adversarial review.
- Do not hand routine implementation to Codex by default.
- Do not start a Claude/Codex loop unless the task truly benefits from one.

## Runtime Rules

- Update `runtime/ACTIVE_TASK.md` when a long task starts, changes phase, or finishes.
- Keep `runtime/QQ_PROGRESS.md` short enough to read comfortably on a phone.
- If the task produces useful deliverables, update `runtime/LAST_ARTIFACTS.json`.
- Respect the current `project_id` / `session_id` and avoid mixing unrelated tasks across sessions.
- If a task comes with attachments, use the local D: paths passed in the task payload.

## Output Rules

- Final answers should be concise Chinese suitable for QQ.
- Mention changed files, artifacts, local URLs, or next steps when they matter.

---
name: phase1-plan
description: Kick off a new Phase 1 remote task with planning, verification, and Codex routing decisions before implementation.
---

# Phase 1 Plan

Use this skill when a new substantial task arrives from NanoBot, QQ, or another remote entrypoint.

## Goals

Before implementation, produce a high-confidence kickoff that answers:

- What is the real task?
- What is the deliverable?
- How will success be verified?
- What are the main risks?
- Should Codex be involved now, later, or not at all?

## Workflow

1. Restate the user goal in plain Chinese.
2. Identify the expected deliverable.
3. Define the verification plan before editing files.
4. Classify the task:
   - Claude-only
   - Claude-led with later Codex review
   - Codex-needed early for planning or hard parts
5. If the task is substantial, stay in planning mode first instead of editing immediately.
6. Update `runtime/ACTIVE_TASK.md` with the current task state.
7. Update `runtime/QQ_PROGRESS.md` with a short kickoff summary suitable for QQ.

## Codex Routing

If the Codex plugin is available:

- Use Codex first for task decomposition, risk analysis, or design pressure testing
- Do not make the first Codex pass a large execution pass by default
- Keep Claude as the main executor unless the task is clearly better owned by Codex

## Output Format

Use these sections in order:

1. Goal
2. Deliverable
3. Verification
4. Risks
5. Codex Decision
6. Next Action

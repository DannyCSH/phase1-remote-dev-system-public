# Heartbeat

This project currently uses the background worker as the primary source of QQ progress updates.

If NanoBot heartbeat is enabled later, it should only send a heartbeat when:

- `runtime/ACTIVE_TASK.md` says a task is still running, and
- `runtime/QQ_PROGRESS.md` contains a phone-friendly update worth forwarding.

Heartbeat messages should stay short and answer four things:

1. Is the task still running normally?
2. What was the latest finished milestone?
3. What is the next step?
4. Is there any visible risk or blocker?

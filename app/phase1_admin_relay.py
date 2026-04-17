from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

from phase1_runtime import (
    ACTIVE_TASK_FILE,
    ADMIN_INBOX_DIR,
    ADMIN_LOCK_FILE,
    ADMIN_TASKS_DIR,
    ARTIFACTS_FILE,
    QQ_PROGRESS_FILE,
    ROOT,
    TMP_DIR,
    append_session_event,
    bind_running_task_state,
    build_receipt,
    ensure_dir,
    ensure_runtime_layout,
    failure_category_from_code,
    first_nonempty_line,
    get_default_project,
    load_phase1_settings,
    merge_task_outcome_state,
    now_iso,
    payload_with_receipt,
    project_execution_dir,
    read_json,
    read_stop_request,
    recent_session_events,
    resolve_project_root,
    update_runtime_state,
    write_json,
)


REMOTE_PROMPT_FILE = ROOT / "prompts" / "nanobot-session.txt"


def _path_from_env(name: str, default: Path) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if raw:
        return Path(raw)
    return default


CLAUDE_FALLBACK = _path_from_env("PHASE1_CLAUDE_PATH", Path.home() / ".local" / "bin" / "claude.exe")
ADMIN_POLL_SECONDS = 3
ADMIN_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
COMMAND_PROBE_TIMEOUT_SECONDS = 8
COMMAND_PROBE_CACHE: dict[tuple[str, ...], bool] = {}
COMMAND_PROBE_CACHE_MAX = 64


def command_is_invocable(command: list[str]) -> bool:
    key = tuple(command)
    cached = COMMAND_PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_PROBE_TIMEOUT_SECONDS,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    if len(COMMAND_PROBE_CACHE) >= COMMAND_PROBE_CACHE_MAX:
        COMMAND_PROBE_CACHE.clear()
    COMMAND_PROBE_CACHE[key] = ok
    return ok


def find_claude_cli() -> str:
    direct_candidate = shutil.which("claude.exe")
    if direct_candidate:
        return direct_candidate
    shim_candidate = shutil.which("claude")
    if CLAUDE_FALLBACK.exists() and command_is_invocable([str(CLAUDE_FALLBACK), "--version"]):
        return str(CLAUDE_FALLBACK)
    if shim_candidate and command_is_invocable([shim_candidate, "--version"]):
        return shim_candidate
    return str(CLAUDE_FALLBACK)


def save_status(task_dir: Path, payload: dict[str, Any]) -> None:
    write_json(task_dir / "status.json", payload)


def build_admin_status_payload(
    *,
    task_id: str,
    phase: str,
    task_name: str,
    project_id: str,
    project_root: str,
    session_key: str,
    session_id: str,
    started_at: str,
    finished_at: str = "",
    result: str = "",
    error: str = "",
    error_type: str = "",
    reply_code: str = "",
    user_visible_status: str = "",
    ack: str = "",
    message: str = "",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_failure_category = failure_category_from_code(error_type)
    resolved_ack = ack or (reply_code or phase or "running")
    resolved_reply_code = reply_code or resolved_ack
    resolved_status = user_visible_status or (
        "failed" if resolved_failure_category else "completed" if phase == "finished" else "running"
    )
    receipt = build_receipt(
        stage="worker",
        ack=resolved_ack,
        message=message or result or error or task_name,
        task_id=task_id,
        session_key=session_key,
        session_id=session_id,
        project_id=project_id,
        project_root=project_root,
        phase=phase,
        reply_code=resolved_reply_code,
        user_visible_status=resolved_status,
        failure_category=resolved_failure_category,
        error_code=error_type,
        error_message=error,
        ts=finished_at or started_at,
        meta=extra_meta,
    )
    payload: dict[str, Any] = {
        "task_id": task_id,
        "phase": phase,
        "started_at": started_at,
        "task_name": task_name,
        "project_id": project_id,
        "project_root": str(project_root),
        "session_key": session_key,
        "session_id": session_id,
    }
    if finished_at:
        payload["finished_at"] = finished_at
    if result:
        payload["result"] = result
    if error:
        payload["error"] = error
    if error_type:
        payload["error_type"] = error_type
    if extra_meta:
        payload["meta"] = extra_meta
    return payload_with_receipt(payload, receipt)


def claim_request() -> tuple[dict[str, Any], str, Path] | None:
    ensure_dir(ADMIN_INBOX_DIR)
    ensure_dir(ADMIN_TASKS_DIR)

    for request_file in sorted(ADMIN_INBOX_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime):
        task_id = request_file.stem
        task_dir = ADMIN_TASKS_DIR / task_id
        ensure_dir(task_dir)
        claimed_path = task_dir / "request.json"
        claimed_path.unlink(missing_ok=True)
        try:
            request_file.replace(claimed_path)
        except FileNotFoundError:
            continue

        payload = read_json(claimed_path, default=None)
        if isinstance(payload, dict):
            task_id = str(payload.get("task_id") or task_id).strip() or task_id
            return payload, task_id, task_dir
        claimed_path.unlink(missing_ok=True)
    return None


def format_recent_context(events: list[dict[str, Any]]) -> str:
    if not events:
        return "无最近上下文。"

    lines: list[str] = []
    for item in events[-8:]:
        kind = str(item.get("type") or "event")
        if kind == "user_enqueued":
            lines.append(f"- 用户补充：{item.get('request', '')}")
        elif kind == "assistant_result":
            lines.append(f"- 上次结果：{item.get('summary', '')}")
        elif kind == "task_status":
            lines.append(f"- 任务状态：{item.get('detail', '')}")
    return "\n".join(lines) if lines else "无最近上下文。"


def build_admin_prompt(request: dict[str, Any], recent_context_text: str) -> str:
    source_task_dir = str(request.get("source_task_dir") or "").strip()
    project_root = str(request.get("project_root") or "").strip()
    allowed_output_dirs = [
        str(Path(source_task_dir) / "artifacts") if source_task_dir else "",
        str(Path(project_root) / ".phase1-artifacts") if project_root else "",
    ]
    allowed_output_dirs = [item for item in allowed_output_dirs if item]

    return textwrap.dedent(
        f"""
        你现在运行在项目 `{ROOT}` 的管理员 relay 里。
        这轮任务真正要操作的项目根目录是 `{project_root}`，默认就在这个目录里执行命令和改文件。
        当前这轮会话已经具备管理员权限。

        你的职责：
        1. 完成这项原本需要管理员权限或系统级改动的任务。
        2. 保持最小必要改动，不要顺手做无关系统修改。
        3. 继续更新 `{ACTIVE_TASK_FILE}` 和 `{QQ_PROGRESS_FILE}`。
        4. 如果要回传文件，只允许把产物登记到 `{ARTIFACTS_FILE}`，并且文件路径必须位于这些目录之一：
           {chr(10).join(f"   - {item}" for item in allowed_output_dirs) if allowed_output_dirs else "   - 无额外白名单目录，请只使用任务目录内产物"}
        5. 不要把任意本地绝对路径伪装成附件或产物，也不要读取不在受控范围内的本地文件。
        6. 对于远程 URL 附件，把它当成链接引用，不要把它当成本地路径。
        7. 最终汇报必须用中文，适合手机 QQ 阅读，先说结果，再说状态，再说风险或下一步。

        当前任务信息：
        - project_id: {request.get("project_id", "")}
        - project_root: {project_root}
        - session_key: {request.get("session_key", "")}
        - session_id: {request.get("session_id", "")}
        - received_at: {request.get("received_at", "")}

        管理员触发原因：
        - reason: {request.get("admin_reason", "")}
        - trigger_note: {request.get("trigger_note", "")}

        最近会话上下文：
        {recent_context_text}

        本轮用户请求：
        {request.get("user_request", "").strip()}

        本轮附件：
        {request.get("attachments_summary", "").strip() or "无附件"}

        Codex 规划结果：
        {str(request.get("codex_plan", "")).strip() or "本轮没有可用的 Codex 规划结果。"}

        现在请直接执行任务，必要时完成系统级步骤，并给出最终中文汇报。
        """
    ).strip()


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
        except Exception:
            pass
        try:
            process.wait(timeout=20)
            return
        except subprocess.TimeoutExpired:
            pass

    try:
        process.terminate()
        process.wait(timeout=20)
        return
    except Exception:
        pass

    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except Exception:
        pass


def run_admin_command(
    command: list[str],
    project_cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
    session_key: str,
    session_id: str,
) -> dict[str, Any]:
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(project_cwd),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        deadline = time.time() + ADMIN_COMMAND_TIMEOUT_SECONDS

        while process.poll() is None:
            time.sleep(ADMIN_POLL_SECONDS)
            if time.time() >= deadline:
                stop_process_tree(process)
                return {
                    "returncode": process.returncode,
                    "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                    "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                    "timed_out": True,
                }
            stop_payload = read_stop_request(session_key, session_id)
            if not stop_payload:
                continue
            stop_process_tree(process)
            return {
                "returncode": process.returncode,
                "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                "stopped": True,
                "stop_payload": stop_payload,
            }

    return {
        "returncode": process.returncode,
        "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
        "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
    }


def resolve_request_project_context(
    request: dict[str, Any],
    config_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], str, str, Path]:
    config = read_json(config_path, default=None, expand_env=True)
    if not isinstance(config, dict):
        raise RuntimeError(f"Invalid config file: {config_path}")

    settings = load_phase1_settings(config)
    default_project_id, default_project_root = get_default_project(config, settings)
    project_id = str(request.get("project_id") or default_project_id).strip() or default_project_id
    raw_project_root = str(request.get("project_root") or "").strip()
    try:
        project_root = resolve_project_root(
            project_id=project_id,
            requested_root=raw_project_root,
            default_project_id=default_project_id,
            default_project_root=default_project_root,
            settings=settings,
        )
    except ValueError as exc:
        raise RuntimeError(f"管理员任务的项目根目录不在允许范围内：{raw_project_root or '<empty>'}") from exc

    request["project_id"] = project_id
    request["project_root"] = project_root
    return config, settings, project_id, project_root, project_execution_dir(project_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config" / "nanobot.local.json"))
    args = parser.parse_args()

    ensure_runtime_layout()

    claimed = claim_request()
    if claimed is None:
        return 0

    request, task_id, task_dir = claimed
    task_name = str(request.get("user_request") or "未命名管理员任务").strip() or "未命名管理员任务"
    started_at = now_iso()
    stdout_path = task_dir / "claude.stdout.log"
    stderr_path = task_dir / "claude.stderr.log"
    session_key = str(request.get("session_key") or "").strip()
    session_id = str(request.get("session_id") or "").strip()
    chat_id = str(request.get("chat_id") or "").strip()
    channel = str(request.get("channel") or "qq").strip() or "qq"
    _, _, project_id, project_root, project_cwd = resolve_request_project_context(
        request,
        Path(args.config),
    )

    write_json(ADMIN_LOCK_FILE, {"task_id": task_id, "pid": os.getpid(), "started_at": started_at})
    save_status(
        task_dir,
        build_admin_status_payload(
            task_id=task_id,
            phase="running",
            task_name=task_name,
            project_id=project_id,
            project_root=str(project_cwd),
            session_key=session_key,
            session_id=session_id,
            started_at=started_at,
            ack="running",
            reply_code="waiting_admin",
            user_visible_status="waiting_admin",
            message="任务已进入管理员执行通道。",
            extra_meta={"admin_reason": request.get("admin_reason", "")},
        ),
    )
    update_runtime_state(
        task_name=task_name,
        status="running",
        progress=f"[管理员通道] {task_name}\n已切到管理员执行通道，正在继续处理。",
        owner="Claude Code (Admin Relay)",
        project_id=project_id,
        session_id=session_id,
        heartbeat_interval_seconds=30 * 60,
        started_at=started_at,
    )

    if session_key and session_id:
        bind_running_task_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=channel,
            default_project_id=project_id or "phase1-remote-dev",
            default_project_root=project_root or str(ROOT),
            project_id=project_id or "phase1-remote-dev",
            project_root=project_root or str(ROOT),
            session_id=session_id,
            task_id=task_id,
        )

    existing_artifacts = read_json(ARTIFACTS_FILE, default=None)
    if not isinstance(existing_artifacts, dict):
        existing_artifacts = {"files": [], "urls": [], "notes": []}
    else:
        def normalize_items(key: str) -> list[str]:
            raw_items = existing_artifacts.get(key, [])
            if not isinstance(raw_items, list):
                return []
            return [str(item).strip() for item in raw_items if str(item).strip()]

        existing_artifacts = {
            "files": normalize_items("files"),
            "urls": normalize_items("urls"),
            "notes": normalize_items("notes"),
        }
    write_json(ARTIFACTS_FILE, existing_artifacts)

    recent_context = format_recent_context(recent_session_events(session_key, session_id, 8)) if session_key and session_id else "无最近上下文。"
    request["attachments_summary"] = request.get("attachments_summary") or ""
    prompt = build_admin_prompt(request, recent_context)
    extra_system_prompt = REMOTE_PROMPT_FILE.read_text(encoding="utf-8") if REMOTE_PROMPT_FILE.exists() else ""
    command = [
        find_claude_cli(),
        "--dangerously-skip-permissions",
        "-p",
        "--output-format",
        "text",
        "--setting-sources",
        "user,project,local",
    ]
    if extra_system_prompt.strip():
        command.extend(["--append-system-prompt", extra_system_prompt])
    command.append(prompt)

    env = os.environ.copy()
    ensure_dir(TMP_DIR)
    env["TEMP"] = str(TMP_DIR)
    env["TMP"] = str(TMP_DIR)
    env["TMPDIR"] = str(TMP_DIR)

    try:
        result = run_admin_command(
            command=command,
            project_cwd=project_cwd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=env,
            session_key=session_key,
            session_id=session_id,
        )
        if result.get("timed_out"):
            raise RuntimeError("管理员 relay 执行超时，进程已被终止。")

        if result.get("stopped"):
            stop_payload = result.get("stop_payload") or {}
            stop_reason = str(stop_payload.get("reason") or "未提供原因").strip()
            stop_message = f"管理员执行通道已按请求停止：{stop_reason}"
            finished_at = now_iso()
            save_status(
                task_dir,
                build_admin_status_payload(
                    task_id=task_id,
                    phase="stopped",
                    task_name=task_name,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    session_key=session_key,
                    session_id=session_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    result=stop_message,
                    ack="stopped",
                    reply_code="admin_relay_stopped",
                    user_visible_status="stopped",
                    message=stop_message,
                    extra_meta={"stop_payload": stop_payload},
                ),
            )
            update_runtime_state(
                task_name=task_name,
                status="stopped",
                progress=f"[管理员通道] {task_name}\n{stop_message}",
                owner="Claude Code (Admin Relay)",
                project_id=project_id,
                session_id=session_id,
                heartbeat_interval_seconds=30 * 60,
                started_at=started_at,
            )
            if session_key and session_id:
                merge_task_outcome_state(
                    session_key=session_key,
                    chat_id=chat_id,
                    channel=channel,
                    default_project_id=project_id or "phase1-remote-dev",
                    default_project_root=project_root or str(ROOT),
                    project_id=project_id or "phase1-remote-dev",
                    project_root=project_root or str(ROOT),
                    session_id=session_id,
                    task_id=task_id,
                    result_text=stop_message,
                    progress="stopped",
                    reply_code="admin_relay_stopped",
                    failure_category="stopped",
                    finished_at=finished_at,
                )
                append_session_event(
                    session_key,
                    session_id,
                    payload_with_receipt(
                        {
                            "ts": finished_at,
                            "type": "task_status",
                            "task_id": task_id,
                            "detail": stop_message,
                            "error_type": "user-stop",
                        },
                        build_receipt(
                            stage="worker",
                            ack="stopped",
                            message=stop_message,
                            task_id=task_id,
                            session_key=session_key,
                            session_id=session_id,
                            project_id=project_id,
                            project_root=str(project_cwd),
                            phase="stopped",
                            reply_code="admin_relay_stopped",
                            user_visible_status="stopped",
                            failure_category="stopped",
                            error_code="user-stop",
                            error_message=stop_reason,
                        ),
                    ),
                )
            return 0

        if int(result.get("returncode", 0)) != 0:
            raise RuntimeError(str(result.get("stderr") or result.get("stdout") or "unknown error").strip())

        final_text = str(result.get("stdout") or "").strip() or "管理员执行通道已完成任务。"
        summary = first_nonempty_line(final_text, "管理员执行通道已完成任务。")
        finished_at = now_iso()
        save_status(
            task_dir,
            build_admin_status_payload(
                task_id=task_id,
                phase="finished",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=started_at,
                finished_at=finished_at,
                result=final_text,
                ack="finished",
                reply_code="completed",
                user_visible_status="completed",
                message=summary,
            ),
        )
        update_runtime_state(
            task_name=task_name,
            status="finished",
            progress=f"[管理员通道] {task_name}\n管理员执行通道已完成任务，结果正在整理回传。",
            owner="Claude Code (Admin Relay)",
            project_id=project_id,
            session_id=session_id,
            heartbeat_interval_seconds=30 * 60,
            started_at=started_at,
        )
        if session_key and session_id:
            merge_task_outcome_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=channel,
                default_project_id=project_id or "phase1-remote-dev",
                default_project_root=project_root or str(ROOT),
                project_id=project_id or "phase1-remote-dev",
                project_root=project_root or str(ROOT),
                session_id=session_id,
                task_id=task_id,
                result_text=summary,
                progress="finished",
                reply_code="completed",
                failure_category="",
                finished_at=finished_at,
            )
            append_session_event(
                session_key,
                session_id,
                payload_with_receipt(
                    {
                        "ts": finished_at,
                        "type": "assistant_result",
                        "task_id": task_id,
                        "summary": summary,
                        "artifacts": read_json(ARTIFACTS_FILE, default={"files": [], "urls": [], "notes": []}),
                    },
                    build_receipt(
                        stage="worker",
                        ack="finished",
                        message=summary,
                        task_id=task_id,
                        session_key=session_key,
                        session_id=session_id,
                        project_id=project_id,
                        project_root=str(project_cwd),
                        phase="finished",
                        reply_code="completed",
                        user_visible_status="completed",
                    ),
                ),
            )
        return 0
    except Exception as exc:
        error_message = f"管理员中继失败：{exc}"
        finished_at = now_iso()
        save_status(
            task_dir,
            build_admin_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type="admin-relay",
                ack="failed",
                reply_code="admin-relay",
                user_visible_status="failed",
                message=error_message,
            ),
        )
        update_runtime_state(
            task_name=task_name,
            status="failed",
            progress=f"[管理员通道] {task_name}\n管理员执行通道失败：{exc}",
            owner="Claude Code (Admin Relay)",
            project_id=project_id,
            session_id=session_id,
            heartbeat_interval_seconds=30 * 60,
            started_at=started_at,
        )
        if session_key and session_id:
            merge_task_outcome_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=channel,
                default_project_id=project_id or "phase1-remote-dev",
                default_project_root=project_root or str(ROOT),
                project_id=project_id or "phase1-remote-dev",
                project_root=project_root or str(ROOT),
                session_id=session_id,
                task_id=task_id,
                result_text=error_message,
                progress="failed",
                reply_code="admin-relay",
                failure_category="admin_relay_failed",
                finished_at=finished_at,
            )
            append_session_event(
                session_key,
                session_id,
                payload_with_receipt(
                    {
                        "ts": finished_at,
                        "type": "task_status",
                        "task_id": task_id,
                        "detail": error_message,
                        "error_type": "admin-relay",
                    },
                    build_receipt(
                        stage="worker",
                        ack="failed",
                        message=error_message,
                        task_id=task_id,
                        session_key=session_key,
                        session_id=session_id,
                        project_id=project_id,
                        project_root=str(project_cwd),
                        phase="failed",
                        reply_code="admin-relay",
                        user_visible_status="failed",
                        failure_category="admin_relay_failed",
                        error_code="admin-relay",
                        error_message=str(exc),
                    ),
                ),
            )
        raise
    finally:
        ADMIN_LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

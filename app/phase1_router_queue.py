from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from phase1_runtime import (
    ROUTER_LOG_DIR,
    WORKER_LOCK_FILE,
    active_session_id,
    append_jsonl,
    append_session_event,
    build_local_file_access_roots,
    build_project_history_reply,
    build_project_history_context,
    build_receipt,
    clear_stop_request,
    create_stop_request,
    detect_freeform_request_intent,
    describe_local_path,
    describe_session_summary,
    ensure_runtime_layout,
    extract_attachments,
    format_file_size,
    get_default_project,
    get_project_state,
    get_session_state,
    interprocess_lock,
    is_path_within_any_root,
    is_pid_alive,
    load_phase1_settings,
    new_session_id,
    new_task_id,
    normalize_allowed_attachment_roots,
    normalize_project_root,
    now_iso,
    parse_control_command,
    project_state_lock_name,
    queue_depth,
    queue_depth_for_session,
    queue_task_path,
    read_json,
    read_project_state,
    register_message_id,
    release_active_session,
    reserve_message_id,
    resolve_user_visible_path,
    resolve_project_root,
    preview_local_file,
    payload_with_receipt,
    save_project_state,
    save_session_state,
    session_state_lock_name,
    summarize_attachments,
    truncate,
    write_json,
)

def _path_from_env(name: str, default: Path) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if raw:
        return Path(raw)
    return default


CLAUDE_FALLBACK = _path_from_env("PHASE1_CLAUDE_PATH", Path.home() / ".local" / "bin" / "claude.exe")
SEMANTIC_ROUTER_TIMEOUT_SECONDS = 25
SEMANTIC_ROUTER_REASONING_EFFORT = "low"
COMMAND_PROBE_TIMEOUT_SECONDS = 8
COMMAND_PROBE_CACHE: dict[tuple[str, ...], bool] = {}
COMMAND_PROBE_CACHE_MAX = 64


def resolve_session_key(
    channel: str,
    chat_id: str,
    sender_id: str,
    raw_session_key: str,
    message_id: str = "",
) -> str:
    normalized_channel = (channel or "qq").strip() or "qq"
    normalized_chat_id = (chat_id or "").strip()
    normalized_sender_id = (sender_id or "").strip()
    session_identity = normalized_chat_id or normalized_sender_id
    if session_identity:
        return f"{normalized_channel}:{session_identity}"
    normalized_message_id = (message_id or "").strip()
    if normalized_message_id:
        return f"{normalized_channel}:msg:{normalized_message_id}"
    return f"{normalized_channel}:anonymous"


def read_worker_lock() -> dict[str, Any]:
    payload = read_json(WORKER_LOCK_FILE, default={}) or {}
    if not isinstance(payload, dict):
        return {}

    pid_raw = str(payload.get("pid") or "").strip()
    if not pid_raw.isdigit():
        return payload

    if not is_pid_alive(pid_raw):
        WORKER_LOCK_FILE.unlink(missing_ok=True)
        return {}
    return payload


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
    raise RuntimeError("claude-missing")


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    candidates = [raw_text]
    for fence in ("```json", "```"):
        start = raw_text.find(fence)
        if start == -1:
            continue
        block = raw_text[start:]
        open_index = block.find("{")
        close_index = block.rfind("}")
        if open_index != -1 and close_index > open_index:
            candidates.append(block[open_index : close_index + 1].strip())

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw_text[start : end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            kind_match = re.search(
                r'["\']?kind["\']?\s*[:=]\s*["\']?(none|project_history|history_analysis|ai_file_search)["\']?',
                candidate,
                flags=re.IGNORECASE,
            )
            wants_send_match = re.search(
                r'["\']?wants_send["\']?\s*[:=]\s*(true|false)',
                candidate,
                flags=re.IGNORECASE,
            )
            if kind_match and wants_send_match:
                return {
                    "kind": kind_match.group(1),
                    "wants_send": wants_send_match.group(1).lower() == "true",
                }
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def build_semantic_router_prompt(user_request: str) -> str:
    return (
        "Classify one authorized QQ message for the Phase 1 router.\n"
        "Reply with one minified JSON object only.\n"
        "Use valid JSON with double quotes around all keys and string values.\n"
        'Allowed kinds: "none", "project_history", "history_analysis", "ai_file_search".\n'
        'Definitions:\n'
        '- "none": normal dev request, chat, or unclear.\n'
        '- "project_history": asking to find or list past projects / records only.\n'
        '- "history_analysis": asking to find past projects / records and also judge optimization or improvement space.\n'
        '- "ai_file_search": asking to search the local computer, disks, folders, files, images, screenshots, resumes, documents, downloads, or desktop.\n'
        'Set "wants_send" to true only if the user wants files/images/docs sent back to QQ or phone.\n'
        'If unsure, choose "none".\n'
        'Examples:\n'
        '{"kind":"project_history","wants_send":false} <- 帮我找一下之前那个 QQ -> NanoBot -> Claude Code -> Codex 的项目记录\n'
        '{"kind":"history_analysis","wants_send":false} <- 我记得以前做过一个手机通过QQ调用电脑AI的工具，这个项目还有优化空间吗\n'
        '{"kind":"ai_file_search","wants_send":true} <- 你帮我在电脑上找找最近更新过的英文简历，找到就发我手机\n'
        '{"kind":"none","wants_send":false} <- 帮我修这个 Python 报错\n'
        f"User message: {user_request.strip()}"
    ).strip()


def semantic_routing_config(settings: dict[str, Any]) -> tuple[bool, int, str]:
    raw = settings.get("semanticRouting") if isinstance(settings.get("semanticRouting"), dict) else {}
    enabled = bool(raw.get("enabled", True))
    timeout_seconds = int(raw.get("timeoutSeconds") or SEMANTIC_ROUTER_TIMEOUT_SECONDS)
    reasoning_effort = str(raw.get("reasoningEffort") or SEMANTIC_ROUTER_REASONING_EFFORT).strip() or SEMANTIC_ROUTER_REASONING_EFFORT
    return enabled, max(timeout_seconds, 5), reasoning_effort


def semantic_classify_authorized_request(
    user_request: str,
    project_root: str,
    settings: dict[str, Any],
    session_key: str,
    chat_id: str,
) -> dict[str, Any] | None:
    enabled, timeout_seconds, reasoning_effort = semantic_routing_config(settings)
    if not enabled or not str(user_request or "").strip():
        return None

    command = [
        find_claude_cli(),
        "-p",
        build_semantic_router_prompt(user_request),
        "--bare",
        "--no-session-persistence",
        "--output-format",
        "text",
        "--effort",
        reasoning_effort,
    ]

    try:
        result = subprocess.run(
            command,
            cwd=str(Path(project_root or ".").resolve(strict=False)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except Exception as exc:
        append_jsonl(
            ROUTER_LOG_DIR / f"router-{time.strftime('%Y%m%d', time.localtime())}.jsonl",
            {
                "ts": now_iso(),
                "type": "semantic_router_error",
                "session_key": session_key,
                "chat_id": chat_id,
                "request": truncate(user_request, 240),
                "error": truncate(str(exc), 240),
            },
        )
        return None

    parsed = extract_json_object(result.stdout)
    if not isinstance(parsed, dict):
        append_jsonl(
            ROUTER_LOG_DIR / f"router-{time.strftime('%Y%m%d', time.localtime())}.jsonl",
            {
                "ts": now_iso(),
                "type": "semantic_router_error",
                "session_key": session_key,
                "chat_id": chat_id,
                "request": truncate(user_request, 240),
                "returncode": result.returncode,
                "stderr": truncate(result.stderr, 240),
                "last_message": truncate(result.stdout, 240),
            },
        )
        return None

    normalized = {
        "kind": str(parsed.get("kind") or "none").strip() or "none",
        "query": str(user_request).strip(),
        "wants_send": bool(parsed.get("wants_send")),
    }
    if normalized["kind"] not in {"none", "project_history", "history_analysis", "ai_file_search"}:
        normalized["kind"] = "none"
    append_jsonl(
        ROUTER_LOG_DIR / f"router-{time.strftime('%Y%m%d', time.localtime())}.jsonl",
        {
            "ts": now_iso(),
            "type": "semantic_router",
            "session_key": session_key,
            "chat_id": chat_id,
            "request": truncate(user_request, 240),
            "kind": normalized["kind"],
            "wants_send": normalized["wants_send"],
            "returncode": result.returncode,
        },
    )
    return normalized


def is_explicit_file_access_authorized(
    config: dict[str, Any],
    channel: str,
    chat_id: str,
    sender_id: str,
    session_key: str,
) -> bool:
    if (channel or "").strip().lower() != "qq":
        return False
    qq_cfg = config.get("channels", {}).get("qq", {}) if isinstance(config.get("channels"), dict) else {}
    raw_allow_from = qq_cfg.get("allowFrom", [])
    if isinstance(raw_allow_from, str):
        allow_from = [raw_allow_from]
    elif isinstance(raw_allow_from, list):
        allow_from = [str(item).strip() for item in raw_allow_from if str(item).strip()]
    else:
        allow_from = []
    if not allow_from:
        return False
    allowed_values = set(allow_from)
    allowed_values.update(f"qq:{item}" for item in allow_from if not str(item).startswith("qq:"))
    return chat_id in allowed_values or sender_id in allowed_values or session_key in allowed_values


def build_attachment_roots(
    raw_task: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
    project_root: str,
) -> list[str]:
    extra_roots: list[str] = []
    qq_cfg = config.get("channels", {}).get("qq", {}) if isinstance(config.get("channels"), dict) else {}
    media_dir = str(qq_cfg.get("mediaDir") or "").strip()
    if media_dir:
        extra_roots.append(media_dir)
    attachment_cfg = settings.get("attachments") if isinstance(settings.get("attachments"), dict) else {}
    configured_roots = attachment_cfg.get("allowedRoots", []) if isinstance(attachment_cfg, dict) else []
    if isinstance(configured_roots, list):
        extra_roots.extend(str(item) for item in configured_roots if str(item).strip())

    return normalize_allowed_attachment_roots(project_root, extra_roots)


def router_user_visible_status(action: str, reply_code: str, failure_category: str) -> str:
    if action == "duplicate":
        return "duplicate"
    if failure_category == "unauthorized_sender":
        return "unauthorized"
    if action == "error":
        return "failed"
    if action == "enqueued":
        return "queued"
    if reply_code in {"invalid_request", "invalid_project_root", "file_not_found", "path_invalid"}:
        return "failed"
    return "completed"


def router_response(
    *,
    action: str,
    reply_text: str,
    session_key: str = "",
    session_id: str = "",
    project_id: str = "",
    project_root: str = "",
    task_id: str = "",
    reply_code: str = "",
    failure_category: str = "",
    routing_mode: str = "",
    queue_depth_value: int | None = None,
    system_action: str = "",
    attachments_summary: str = "",
    queue_file: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_reply_code = reply_code or action
    resolved_status = router_user_visible_status(action, resolved_reply_code, failure_category)
    receipt = build_receipt(
        stage="router",
        ack=action,
        message=reply_text,
        task_id=task_id,
        session_key=session_key,
        session_id=session_id,
        project_id=project_id,
        project_root=project_root,
        phase=action,
        reply_code=resolved_reply_code,
        user_visible_status=resolved_status,
        failure_category=failure_category,
        meta={
            **(meta or {}),
            "routing_mode": routing_mode,
            "queue_depth": queue_depth_value,
            "system_action": system_action,
        },
    )
    payload = {
        "action": action,
        "reply_text": reply_text,
        "task_id": task_id,
        "project_id": project_id,
        "project_root": project_root,
        "session_key": session_key,
        "session_id": session_id,
    }
    if routing_mode:
        payload["routing_mode"] = routing_mode
    if queue_depth_value is not None:
        payload["queue_depth"] = queue_depth_value
    if system_action:
        payload["system_action"] = system_action
    if attachments_summary:
        payload["attachments_summary"] = attachments_summary
    if queue_file:
        payload["queue_file"] = queue_file
    return payload_with_receipt(payload, receipt)


def format_allowed_roots_for_reply(allowed_roots: list[str], limit: int = 4) -> str:
    shown = [str(item) for item in allowed_roots[: max(limit, 1)]]
    if not shown:
        return "当前没有可用的授权目录。"
    lines = [f"{index}. {root}" for index, root in enumerate(shown, 1)]
    if len(allowed_roots) > len(shown):
        lines.append(f"… 另外还有 {len(allowed_roots) - len(shown)} 个根目录")
    return "\n".join(lines)


def route_task(task_file: Path, config_path: Path) -> dict[str, Any]:
    ensure_runtime_layout()

    raw_task = read_json(task_file, default=None)
    config = read_json(config_path, default={}, expand_env=True) or {}
    if not isinstance(raw_task, dict):
        return router_response(
            action="error",
            reply_text="收到的任务文件不是合法 JSON，已忽略这条任务。",
            reply_code="invalid_request",
            failure_category="router_failed",
        )

    settings = load_phase1_settings(config)
    default_project_id, default_project_root = get_default_project(config, settings)

    channel = str(raw_task.get("channel") or "qq").strip() or "qq"
    chat_id = str(raw_task.get("chat_id") or "").strip()
    sender_id = str(raw_task.get("sender_id") or chat_id).strip()
    message_id = str(raw_task.get("message_id") or "").strip()
    session_key = resolve_session_key(
        channel,
        chat_id,
        sender_id,
        str(raw_task.get("session_key") or ""),
        message_id,
    )
    received_at = str(raw_task.get("received_at") or now_iso()).strip()
    user_request = str(raw_task.get("user_request") or raw_task.get("content") or "").strip()
    metadata = dict(raw_task.get("metadata") or {}) if isinstance(raw_task.get("metadata"), dict) else {}
    phase1_metadata = dict(metadata.get("phase1") or {}) if isinstance(metadata.get("phase1"), dict) else {}
    command = parse_control_command(user_request)
    freeform_intent = detect_freeform_request_intent(user_request) if command["kind"] == "none" else None
    authorized_semantic_sender = is_explicit_file_access_authorized(config, channel, chat_id, sender_id, session_key)
    if command["kind"] == "none" and freeform_intent is None and authorized_semantic_sender:
        semantic_intent = semantic_classify_authorized_request(
            user_request=user_request,
            project_root=default_project_root,
            settings=settings,
            session_key=session_key,
            chat_id=chat_id,
        )
        if semantic_intent and semantic_intent.get("kind") != "none":
            freeform_intent = semantic_intent

    explicit_system_action = ""
    explicit_system_payload: dict[str, Any] | None = None

    with interprocess_lock(session_state_lock_name(session_key), timeout_seconds=15):
        session_state = get_session_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=channel,
            default_project_id=default_project_id,
            default_project_root=default_project_root,
        )

        recent_limit = int(settings["session"].get("recentMessageIdLimit") or 64)
        message_ttl = int(settings["session"].get("globalMessageIdTtlSeconds") or 7 * 24 * 60 * 60)
        duplicate_via_global_marker = reserve_message_id(session_key, message_id, message_ttl)
        duplicate_via_recent_window = register_message_id(session_state, message_id, recent_limit)
        if duplicate_via_global_marker or duplicate_via_recent_window:
            save_session_state(session_key, session_state)
            return router_response(
                action="duplicate",
                reply_text="这条消息已经处理过了，我就不重复建任务了。",
                session_key=session_key,
                project_id=str(session_state.get("current_project_id", default_project_id)),
                session_id=str(session_state.get("current_session_id") or ""),
                reply_code="duplicate",
                failure_category="duplicate",
            )

        current_project_id = str(session_state.get("current_project_id") or default_project_id).strip()
        requested_project_id = str(raw_task.get("project_id") or "").strip()
        requested_project_root_text = str(raw_task.get("project_root") or "").strip()
        normalized_requested_root = normalize_project_root(requested_project_root_text)
        normalized_default_root = normalize_project_root(default_project_root)
        force_project_from_payload = any(
            bool(value)
            for value in (
                raw_task.get("force_project"),
                metadata.get("force_project"),
                metadata.get("explicit_project"),
                phase1_metadata.get("force_project"),
                phase1_metadata.get("explicit_project"),
            )
        )
        payload_requests_non_default_project = bool(
            requested_project_id and requested_project_id != default_project_id
        )
        payload_requests_custom_root = bool(
            normalized_requested_root and normalized_requested_root != normalized_default_root
        )
        explicit_project_from_payload = force_project_from_payload or bool(
            requested_project_id
            and (
                not message_id
                or payload_requests_non_default_project
                or payload_requests_custom_root
            )
        )
        if explicit_project_from_payload and requested_project_id:
            project_id = requested_project_id
        else:
            project_id = str(current_project_id or requested_project_id or default_project_id).strip()
        requested_project_root = requested_project_root_text if normalized_requested_root else ""

        try:
            project_root = resolve_project_root(
                project_id=project_id,
                requested_root=requested_project_root,
                default_project_id=default_project_id,
                default_project_root=default_project_root,
                settings=settings,
            )
        except ValueError:
            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text="这个项目根目录不在允许范围内，我没有为它创建任务。请改成受控工作区目录后再试。",
                session_key=session_key,
                project_id=current_project_id or default_project_id,
                session_id=str(session_state.get("current_session_id") or ""),
                reply_code="invalid_project_root",
                failure_category="router_failed",
            )

        if phase1_metadata.get("health_probe"):
            phase1_metadata["health_probe"] = True
            phase1_metadata["health_probe_source"] = str(
                phase1_metadata.get("health_probe_source") or "Test-Phase1Pipeline.ps1"
            ).strip() or "Test-Phase1Pipeline.ps1"
            phase1_metadata["health_probe_requested_at"] = str(
                phase1_metadata.get("health_probe_requested_at") or received_at
            ).strip() or received_at
            metadata["phase1"] = phase1_metadata
            explicit_system_action = "health_probe"
            explicit_system_payload = {
                "source": phase1_metadata["health_probe_source"],
                "requested_at": phase1_metadata["health_probe_requested_at"],
            }

        if command["kind"] == "project_history":
            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text=build_project_history_reply(command["value"]),
                session_key=session_key,
                project_id=project_id,
                session_id=str(session_state.get("current_session_id") or ""),
                reply_code="project_history",
            )

        if command["kind"] in {"browse_path", "read_file", "send_file"}:
            if not is_explicit_file_access_authorized(config, channel, chat_id, sender_id, session_key):
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="当前聊天没有被授权使用本地文件浏览或回传命令。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="unauthorized_sender",
                    failure_category="unauthorized_sender",
                )

            allowed_file_roots = build_local_file_access_roots(
                config=config,
                settings=settings,
                project_root=project_root,
                default_project_root=default_project_root,
            )

            try:
                requested_path = resolve_user_visible_path(command["value"], base_root=project_root)
            except ValueError:
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="没有收到可用的文件路径。请发送“发送文件：绝对路径”或“发送文件：相对路径”。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="path_invalid",
                    failure_category="router_failed",
                )

            if not is_path_within_any_root(requested_path, allowed_file_roots):
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=(
                        f"这个路径不在当前授权范围内：{requested_path}\n"
                        "当前允许访问的根目录：\n"
                        f"{format_allowed_roots_for_reply(allowed_file_roots)}"
                    ),
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="path_outside_allowed_roots",
                    failure_category="router_failed",
                )

            if command["kind"] == "browse_path":
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=describe_local_path(str(requested_path), base_root=project_root),
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="browse_path",
                )

            if command["kind"] == "read_file":
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=preview_local_file(str(requested_path), base_root=project_root),
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="read_file",
                )

            try:
                requested_file = resolve_user_visible_path(command["value"], base_root=project_root)
            except ValueError:
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="没有收到可用的文件路径。请发送“发送文件：绝对路径”或“发送文件：相对路径”。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="path_invalid",
                    failure_category="router_failed",
                )

            if not requested_file.exists():
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=f"文件不存在：{requested_file}",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="file_not_found",
                    failure_category="router_failed",
                )
            if not requested_file.is_file():
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=f"这不是文件：{requested_file}。如果你是想看目录，请用“查看目录：{requested_file}”。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="not_a_file",
                    failure_category="router_failed",
                )

            max_send_bytes = int(settings["artifacts"].get("maxTotalBytes") or 80 * 1024 * 1024)
            file_size = requested_file.stat().st_size
            if file_size > max_send_bytes:
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=(
                        f"这个文件有 {format_file_size(file_size)}，超过当前直发上限 {format_file_size(max_send_bytes)}。"
                        " 你可以先手动压缩，或者之后我再帮你做分卷/打包方案。"
                    ),
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="file_too_large",
                    failure_category="artifact_send_failed",
                )

            explicit_system_action = "send_local_file"
            explicit_system_payload = {
                "path": str(requested_file),
                "size_bytes": file_size,
                "allowed_roots": allowed_file_roots,
            }
            user_request = f"发送本地文件到手机：{requested_file}"

        if freeform_intent and explicit_system_action != "health_probe":
            if freeform_intent["kind"] == "project_history":
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text=build_project_history_reply(
                        freeform_intent["query"],
                        limit=int(settings.get("computerSearch", {}).get("historyMatchLimit") or 8),
                    ),
                    session_key=session_key,
                    project_id=project_id,
                    session_id=str(session_state.get("current_session_id") or ""),
                    reply_code="project_history",
                )

            if freeform_intent["kind"] == "history_analysis":
                phase1_metadata["history_query"] = freeform_intent["query"]
                phase1_metadata["history_context"] = build_project_history_context(
                    freeform_intent["query"],
                    limit=int(settings.get("computerSearch", {}).get("historyMatchLimit") or 5),
                )
                metadata["phase1"] = phase1_metadata
                explicit_system_action = explicit_system_action or "history_analysis"

            if freeform_intent["kind"] == "ai_file_search":
                if not is_explicit_file_access_authorized(config, channel, chat_id, sender_id, session_key):
                    save_session_state(session_key, session_state)
                    return router_response(
                        action="local_replied",
                        reply_text="当前聊天没有被授权执行全机本地文件搜索或文件回传。",
                        session_key=session_key,
                        project_id=project_id,
                        session_id=str(session_state.get("current_session_id") or ""),
                        reply_code="unauthorized_sender",
                        failure_category="unauthorized_sender",
                    )

                allowed_search_roots = build_local_file_access_roots(
                    config=config,
                    settings=settings,
                    project_root=project_root,
                    default_project_root=default_project_root,
                )
                phase1_metadata["authorized_computer_search"] = True
                phase1_metadata["computer_search_query"] = freeform_intent["query"]
                phase1_metadata["computer_search_roots"] = allowed_search_roots
                phase1_metadata["computer_search_wants_send"] = bool(freeform_intent.get("wants_send"))
                metadata["phase1"] = phase1_metadata
                explicit_system_action = explicit_system_action or "authorized_ai_file_search"
                explicit_system_payload = explicit_system_payload or {
                    "query": freeform_intent["query"],
                    "allowed_roots": allowed_search_roots,
                    "wants_send": bool(freeform_intent.get("wants_send")),
                }

        attachments = extract_attachments(
            raw_task,
            allowed_roots=build_attachment_roots(raw_task, config, settings, project_root),
        )
        current_session_id = str(session_state.get("current_session_id") or new_session_id()).strip()
        running_session_id = active_session_id(session_state)
        worker_lock = read_worker_lock()
        session_active_task_id = str(session_state.get("active_task_id") or "").strip()
        if running_session_id and session_active_task_id and not worker_lock:
            release_active_session(session_state, running_session_id)
            running_session_id = ""
            session_active_task_id = ""
            save_session_state(session_key, session_state)

        worker_session_key = str(worker_lock.get("session_key") or "").strip()
        worker_session_id = str(worker_lock.get("session_id") or "").strip()
        if worker_session_key and worker_session_key == session_key and worker_session_id == running_session_id:
            active_task_id = str(
                worker_lock.get("active_task_id")
                or worker_lock.get("task_id")
                or session_active_task_id
            ).strip()
        else:
            active_task_id = session_active_task_id

        if command["kind"] == "switch_project":
            project_id = command["value"]
            try:
                project_root = resolve_project_root(
                    project_id=project_id,
                    requested_root=str(raw_task.get("project_root") or "").strip(),
                    default_project_id=default_project_id,
                    default_project_root=default_project_root,
                    settings=settings,
                )
            except ValueError:
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="这个项目根目录不在允许范围内，我没有切换项目。",
                    session_key=session_key,
                    project_id=str(session_state.get("current_project_id") or default_project_id),
                    session_id=current_session_id,
                    reply_code="invalid_project_root",
                    failure_category="router_failed",
                )

            current_session_id = new_session_id()
            session_state["current_project_id"] = project_id
            session_state["current_project_root"] = project_root
            session_state["current_session_id"] = current_session_id
            if not active_task_id:
                release_active_session(session_state)
            save_session_state(session_key, session_state)

            with interprocess_lock(project_state_lock_name(project_id, project_root), timeout_seconds=15):
                project_state = get_project_state(project_id, project_root, session_key, current_session_id)
                project_state["current_session_key"] = session_key
                project_state["current_session_id"] = current_session_id
                save_project_state(project_id, project_state)
            return router_response(
                action="local_replied",
                reply_text=f"已切换到项目“{project_id}”。当前项目根目录：{project_root}",
                session_key=session_key,
                session_id=current_session_id,
                project_id=project_id,
                project_root=project_root,
                reply_code="project_switched",
            )

        if command["kind"] == "reset_session":
            old_session_id = running_session_id or current_session_id
            old_queue_count = queue_depth_for_session(session_key, old_session_id, project_id, project_root) if old_session_id else 0
            current_session_id = new_session_id()
            session_state["current_session_id"] = current_session_id
            if active_task_id or old_queue_count > 0:
                if old_session_id:
                    create_stop_request(
                        session_key=session_key,
                        session_id=old_session_id,
                        reason="QQ 命令：重置会话，停止旧会话中的任务",
                        requested_by=sender_id or chat_id or session_key,
                    )
            else:
                release_active_session(session_state)
            save_session_state(session_key, session_state)
            reply_text = "当前会话已重置，后续我会按新的上下文来处理。"
            if active_task_id or old_queue_count > 0:
                reply_text += " 旧会话里尚未完成的任务也已经登记停止请求。"
            return router_response(
                action="local_replied",
                reply_text=reply_text,
                session_key=session_key,
                project_id=project_id,
                session_id=current_session_id,
                reply_code="session_reset",
            )

        if command["kind"] == "summarize_current":
            project_state = read_project_state(project_id, project_root)
            session_queue_count = queue_depth_for_session(session_key, current_session_id, project_id, project_root)
            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text=describe_session_summary(
                    session_state=session_state,
                    project_state=project_state if isinstance(project_state, dict) else None,
                    queue_count=session_queue_count,
                    active_task_id=active_task_id,
                ),
                session_key=session_key,
                project_id=project_id,
                session_id=current_session_id,
                reply_code="session_summary",
            )

        if command["kind"] == "stop_current":
            queued_current = queue_depth_for_session(session_key, current_session_id, project_id, project_root)
            queued_running = 0
            if running_session_id and running_session_id != current_session_id:
                queued_running = queue_depth_for_session(
                    session_key,
                    running_session_id,
                    project_id,
                    project_root,
                )

            if active_task_id:
                stop_session_id = running_session_id or current_session_id
                create_stop_request(
                    session_key=session_key,
                    session_id=stop_session_id,
                    reason="QQ 命令：停止当前任务",
                    requested_by=sender_id or chat_id or session_key,
                )
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="已发出停止请求。如果当前有任务正在运行，我会尽快停下来并回报状态。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=current_session_id,
                    reply_code="stop_requested",
                )

            if queued_current > 0 and current_session_id:
                create_stop_request(
                    session_key=session_key,
                    session_id=current_session_id,
                    reason="QQ 命令：停止当前排队任务",
                    requested_by=sender_id or chat_id or session_key,
                )
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="已记录停止请求。当前任务还在排队，等它真正开始时我会立即停掉。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=current_session_id,
                    reply_code="stop_requested",
                )

            if queued_running > 0 and running_session_id:
                create_stop_request(
                    session_key=session_key,
                    session_id=running_session_id,
                    reason="QQ 命令：停止旧会话中的排队任务",
                    requested_by=sender_id or chat_id or session_key,
                )
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="已记录停止请求。旧会话里仍在排队的任务会在启动时立刻停止。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=current_session_id,
                    reply_code="stop_requested",
                )

            for candidate_session_id in {current_session_id, running_session_id}:
                if candidate_session_id:
                    clear_stop_request(session_key, candidate_session_id)
            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text="当前没有正在运行或等待中的任务，所以我没有保留停止请求。",
                session_key=session_key,
                project_id=project_id,
                session_id=current_session_id,
                reply_code="stop_not_needed",
            )

        if command["kind"] == "continue_current":
            queued_current = queue_depth_for_session(session_key, current_session_id, project_id, project_root)
            if active_task_id and running_session_id:
                clear_stop_request(session_key, running_session_id)
                session_state["current_session_id"] = running_session_id
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="会继续沿用当前正在运行的会话。你直接补充需求就行。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=running_session_id,
                    reply_code="continue_session",
                )

            if queued_current > 0:
                clear_stop_request(session_key, current_session_id)
                save_session_state(session_key, session_state)
                return router_response(
                    action="local_replied",
                    reply_text="会继续沿用当前项目和当前会话，已取消这个会话上的停止请求。",
                    session_key=session_key,
                    project_id=project_id,
                    session_id=current_session_id,
                    reply_code="continue_session",
                )

            if running_session_id and running_session_id != current_session_id:
                queued_running = queue_depth_for_session(
                    session_key,
                    running_session_id,
                    project_id,
                    project_root,
                )
                if queued_running > 0:
                    clear_stop_request(session_key, running_session_id)
                    session_state["current_session_id"] = running_session_id
                    save_session_state(session_key, session_state)
                    return router_response(
                        action="local_replied",
                        reply_text="已切回仍在排队的旧会话，并取消该会话上的停止请求。",
                        session_key=session_key,
                        project_id=project_id,
                        session_id=running_session_id,
                        reply_code="continue_session",
                    )

            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text="会继续沿用当前项目和当前会话。你直接补充需求就行。",
                session_key=session_key,
                project_id=project_id,
                session_id=current_session_id,
                reply_code="continue_session",
            )

        if command["kind"] == "new_task":
            current_session_id = new_session_id()
            user_request = command["request_text"]

        if not user_request:
            save_session_state(session_key, session_state)
            return router_response(
                action="local_replied",
                reply_text="这条消息里还没有可执行的开发需求。你可以直接补一句具体要我做什么。",
                session_key=session_key,
                project_id=project_id,
                session_id=current_session_id,
                reply_code="no_actionable_request",
                failure_category="router_failed",
            )

        task_id = str(raw_task.get("task_id") or new_task_id(channel)).strip() or new_task_id(channel)
        routing_mode = "flush" if attachments or command["flush"] or explicit_system_action == "health_probe" else "collect"
        normalized_task = {
            "task_id": task_id,
            "channel": channel,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "message_id": message_id,
            "received_at": received_at,
            "received_ts": time.time(),
            "user_request": user_request,
            "project_id": project_id,
            "project_root": project_root,
            "session_key": session_key,
            "session_id": current_session_id,
            "session_action": command["kind"] if command["kind"] != "none" else "continue",
            "routing_mode": routing_mode,
            "attachments": attachments,
            "is_group": bool(raw_task.get("is_group", False)),
            "metadata": metadata,
            "source_task_file": str(task_file),
        }
        if explicit_system_action:
            normalized_task["system_action"] = explicit_system_action
            normalized_task["system_payload"] = explicit_system_payload or {}
        queue_path = queue_task_path(normalized_task)
        write_json(queue_path, normalized_task)

        running_same_chat_task = bool(active_task_id) and worker_session_key == session_key
        session_state["current_project_id"] = project_id
        session_state["current_project_root"] = project_root
        session_state["current_session_id"] = current_session_id
        if not running_same_chat_task:
            release_active_session(session_state)
        session_state["last_inbound_at"] = received_at
        session_state["last_enqueued_task_id"] = task_id
        save_session_state(session_key, session_state)

        with interprocess_lock(project_state_lock_name(project_id, project_root), timeout_seconds=15):
            project_state = get_project_state(project_id, project_root, session_key, current_session_id)
            project_state["current_session_key"] = session_key
            project_state["current_session_id"] = current_session_id
            project_state["last_task_id"] = task_id
            save_project_state(project_id, project_state)

    append_session_event(
        session_key=session_key,
        session_id=current_session_id,
        payload={
            "ts": now_iso(),
            "type": "user_enqueued",
            "task_id": task_id,
            "project_id": project_id,
            "message_id": message_id,
            "request": user_request,
            "attachments": attachments,
        },
    )
    append_jsonl(
        ROUTER_LOG_DIR / f"router-{time.strftime('%Y%m%d', time.localtime())}.jsonl",
        {
            "ts": now_iso(),
            "type": "enqueue",
            "task_id": task_id,
            "project_id": project_id,
            "session_key": session_key,
            "session_id": current_session_id,
            "routing_mode": routing_mode,
            "attachments": attachments,
            "request": truncate(user_request, 240),
        },
    )

    attachment_note = ""
    if attachments:
        attachment_note = "\n附件已写入受控输入范围并一起入队。"

    reply_text = f"已收下任务，正在按当前项目与会话排队处理。{attachment_note}".strip()
    if explicit_system_action == "send_local_file" and explicit_system_payload:
        reply_text = (
            f"已接收文件回传请求，准备把这个文件发到 QQ：{explicit_system_payload['path']}"
        )
    elif explicit_system_action == "health_probe":
        reply_text = "已接收 Phase 1 健康探针，正在走秒级自检通道。"
    elif explicit_system_action == "authorized_ai_file_search":
        reply_text = "已识别为全机文件搜索请求，接下来会进入 Claude Code / Codex 的联合处理链路来理解你的自然语言条件并执行搜索。"
    elif explicit_system_action == "history_analysis":
        reply_text = "已识别为历史项目追溯请求，接下来会结合历史记录和当前项目上下文，进入 Claude Code / Codex 的联合处理链路继续分析优化空间。"

    return router_response(
        action="enqueued",
        reply_text=reply_text,
        task_id=task_id,
        project_id=project_id,
        project_root=project_root,
        session_key=session_key,
        session_id=current_session_id,
        reply_code="queued",
        routing_mode=routing_mode,
        queue_file=str(queue_path),
        queue_depth_value=queue_depth(),
        attachments_summary=summarize_attachments(attachments),
        system_action=explicit_system_action,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    result = route_task(Path(args.task_file), Path(args.config))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("action") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())

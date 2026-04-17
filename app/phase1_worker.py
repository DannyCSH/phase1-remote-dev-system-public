from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

from phase1_runtime import (
    ACTIVE_TASK_FILE,
    ADMIN_LOCK_FILE,
    ADMIN_INBOX_DIR,
    ADMIN_TASKS_DIR,
    ARTIFACTS_FILE,
    LOGS_DIR,
    QUEUE_PENDING_DIR,
    QUEUE_PROCESSING_DIR,
    QQ_PROGRESS_FILE,
    ROOT,
    RUNTIME_DIR,
    TASKS_DIR,
    TMP_DIR,
    WORKER_LOCK_FILE,
    bind_running_task_state,
    bind_active_session,
    append_jsonl,
    append_session_event,
    archive_claimed_queue_file,
    build_local_file_access_roots,
    build_receipt,
    claim_matching_pending_tasks,
    claim_next_pending_task,
    clear_stop_request,
    ensure_dir,
    ensure_runtime_layout,
    failure_category_from_code,
    first_nonempty_line,
    format_file_size,
    format_progress,
    get_default_project,
    get_project_state,
    get_session_state,
    guess_file_type,
    is_pid_alive,
    load_phase1_settings,
    merge_task_outcome_state,
    new_task_id,
    now_iso,
    normalize_qq_text,
    package_artifacts_for_qq,
    project_execution_dir,
    is_path_within_any_root,
    qq_upload_size_allowed,
    queue_task_path,
    queue_depth,
    queue_processing_files,
    read_json,
    read_stop_request,
    recent_session_events,
    release_active_session,
    resolve_project_root,
    restore_queue_file,
    payload_with_receipt,
    save_project_state,
    save_session_state,
    split_qq_text,
    summarize_attachments,
    truncate,
    update_runtime_state,
    write_json,
    write_text,
)


REMOTE_PROMPT_FILE = ROOT / "prompts" / "nanobot-session.txt"
ADMIN_RELAY_TRIGGER = ROOT / "scripts" / "Request-Phase1AdminRelay.ps1"


def _path_from_env(name: str, default: Path) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if raw:
        return Path(raw)
    return default


def _default_npm_bin_dir() -> Path:
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "npm"
    return Path.home() / "AppData" / "Roaming" / "npm"


CODEX_COMPANION = _path_from_env(
    "PHASE1_CODEX_COMPANION_PATH",
    ROOT / "vendor" / "codex-plugin-cc" / "plugins" / "codex" / "scripts" / "codex-companion.mjs",
)
PREFERRED_CODEX_BIN_DIR = _path_from_env("PHASE1_CODEX_BIN_DIR", _default_npm_bin_dir())
PREFERRED_CODEX_CMD = PREFERRED_CODEX_BIN_DIR / "codex.cmd"
PREFERRED_CODEX_PWSH = PREFERRED_CODEX_BIN_DIR / "codex.ps1"
PREFERRED_CODEX_JS = PREFERRED_CODEX_BIN_DIR / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
CLAUDE_FALLBACK = _path_from_env("PHASE1_CLAUDE_PATH", Path.home() / ".local" / "bin" / "claude.exe")
CODEX_PLAN_TIMEOUT_SECONDS = 300
CODEX_REVIEW_TIMEOUT_SECONDS = 900
CODEX_PLAN_REASONING_EFFORT = "medium"
CODEX_REVIEW_REASONING_EFFORT = "medium"
ADMIN_RELAY_TIMEOUT_SECONDS = 2 * 60 * 60
ADMIN_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
ADMIN_REQUEST_FILE_NAME = "admin-escalation.json"
WORKER_POLL_SECONDS = 3
ADMIN_DECISION_PATTERN = re.compile(r"ADMIN_REQUIRED:\s*(yes|no|maybe)", re.I)
CODEx_REVIEW_FINDING_PATTERN = re.compile(r"^- \[(P\d+)\]\s+(.+?)\s+[—-]\s+(.+)$")
CODEX_REVIEW_LOCATION_PATTERN = re.compile(r"^(?P<path>.+):(?P<line>\d+)(?:-\d+)?$")


REVIEW_SNAPSHOT_TOP_LEVEL_EXCLUDES = {
    ".git",
    "runtime",
    "vendor",
    "node_modules",
    ".venv",
    "venv",
}
REVIEW_SNAPSHOT_INNER_EXCLUDES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "vendor",
    "node_modules",
    ".venv",
    "venv",
}
SYNTHETIC_REVIEW_SOURCE_MARKERS = (
    "\\runtime\\test-",
    "\\runtime\\test\\",
    "\\runtime\\tmp\\",
    "\\runtime\\queue\\",
    "\\runtime\\tasks\\",
    "\\runtime\\sessions\\",
    "\\ai_temp\\",
    "phase1-review",
    "test-switch",
    "test-follow",
)
SYNTHETIC_REVIEW_SESSION_PREFIXES = (
    "qq:test-",
    "qq:u",
    "qq:t",
)
COMMAND_PROBE_TIMEOUT_SECONDS = 8
COMMAND_PROBE_CACHE: dict[tuple[str, ...], bool] = {}
COMMAND_PROBE_CACHE_MAX = 64


class Phase1Error(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def resolve_task_project_context(
    task: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[str, str, Path, str]:
    default_project_id, default_project_root = get_default_project(config, settings)
    project_id = str(task.get("project_id") or default_project_id).strip() or default_project_id
    raw_project_root = str(task.get("project_root") or "").strip()
    try:
        project_root = resolve_project_root(
            project_id=project_id,
            requested_root=raw_project_root,
            default_project_id=default_project_id,
            default_project_root=default_project_root,
            settings=settings,
        )
    except ValueError as exc:
        raise Phase1Error(
            "invalid_project_root",
            f"任务携带的项目根目录不在允许范围内：{raw_project_root or '<empty>'}",
        ) from exc
    return project_id, project_root, project_execution_dir(project_root), default_project_root


def trusted_authorized_search_roots(
    task: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
    project_root: str,
    default_project_root: str = "",
) -> list[str]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    if str(task.get("system_action") or "").strip() != "authorized_ai_file_search" and not phase1_metadata.get(
        "authorized_computer_search"
    ):
        return []
    return build_local_file_access_roots(
        config=config,
        settings=settings,
        project_root=project_root,
        default_project_root=default_project_root,
    )


def apply_trusted_search_roots(task: dict[str, Any], trusted_roots: list[str]) -> dict[str, Any]:
    if not trusted_roots:
        return task
    metadata = dict(task.get("metadata") if isinstance(task.get("metadata"), dict) else {})
    phase1_metadata = dict(metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {})
    phase1_metadata["authorized_computer_search"] = True
    phase1_metadata["computer_search_roots"] = trusted_roots
    metadata["phase1"] = phase1_metadata
    task["metadata"] = metadata
    return task


def reset_artifacts() -> None:
    write_json(ARTIFACTS_FILE, {"files": [], "urls": [], "notes": []})


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


def build_seq(offset: int = 0) -> int:
    return int(time.time() * 1000) % 1_000_000_000 + offset


def is_group_task(task: dict[str, Any]) -> bool:
    return bool(task.get("is_group", False))


def qq_send_text(channel_cfg: dict[str, Any], chat_id: str, text: str, is_group: bool = False) -> None:
    from botpy.api import BotAPI
    from botpy.http import BotHttp

    async def _send() -> None:
        http = BotHttp(timeout=120, app_id=channel_cfg["appId"], secret=channel_cfg["secret"])
        await http.check_session()
        api = BotAPI(http)
        try:
            for index, chunk in enumerate(split_qq_text(text)):
                seq = build_seq(index)
                if is_group:
                    await api.post_group_message(group_openid=chat_id, msg_type=0, content=chunk, msg_seq=seq)
                else:
                    await api.post_c2c_message(openid=chat_id, msg_type=0, content=chunk, msg_seq=seq)
        finally:
            await http.close()

    if text.strip():
        asyncio.run(_send())


def qq_send_files(
    channel_cfg: dict[str, Any],
    chat_id: str,
    files: list[str],
    is_group: bool = False,
    inter_file_delay_ms: int = 0,
) -> None:
    from botpy.api import BotAPI
    from botpy.http import BotHttp, Route

    def encode_file_base64(path: Path, chunk_size: int = 256 * 1024) -> str:
        # Stream the file to avoid holding both raw bytes and the full
        # base64-encoded payload in memory at the same time.
        parts: list[str] = []
        carry = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                data = carry + chunk
                remainder = len(data) % 3
                if remainder:
                    carry = data[-remainder:]
                    data = data[:-remainder]
                else:
                    carry = b""
                if data:
                    parts.append(base64.b64encode(data).decode("ascii"))
        if carry:
            parts.append(base64.b64encode(carry).decode("ascii"))
        return "".join(parts)

    async def _send() -> None:
        http = BotHttp(timeout=120, app_id=channel_cfg["appId"], secret=channel_cfg["secret"])
        await http.check_session()
        api = BotAPI(http)
        errors: list[str] = []
        try:
            for index, file_name in enumerate(files):
                path = Path(file_name)
                if not path.is_file():
                    continue

                try:
                    endpoint = "/v2/groups/{group_openid}/files" if is_group else "/v2/users/{openid}/files"
                    id_key = "group_openid" if is_group else "openid"
                    file_type = guess_file_type(path)
                    payload: dict[str, Any] = {
                        id_key: chat_id,
                        "file_type": file_type,
                        "file_data": encode_file_base64(path),
                        "srv_send_msg": False,
                    }
                    if file_type != 1:
                        payload["file_name"] = path.name

                    route = Route("POST", endpoint, **{id_key: chat_id})
                    result = await http.request(route, json=payload)
                    media = {"file_info": result["file_info"]} if isinstance(result, dict) and "file_info" in result else result
                    seq = build_seq(index)

                    if is_group:
                        await api.post_group_message(group_openid=chat_id, msg_type=7, media=media, msg_seq=seq)
                    else:
                        await api.post_c2c_message(openid=chat_id, msg_type=7, media=media, msg_seq=seq)
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                if inter_file_delay_ms > 0 and index < len(files) - 1:
                    await asyncio.sleep(inter_file_delay_ms / 1000.0)
        finally:
            await http.close()
        if errors:
            raise RuntimeError("; ".join(errors))

    if files:
        asyncio.run(_send())


def try_qq_send_text(channel_cfg: dict[str, Any], chat_id: str, text: str, is_group: bool = False) -> str | None:
    try:
        qq_send_text(channel_cfg, chat_id, text, is_group=is_group)
    except Exception as exc:
        return str(exc)
    return None


def try_qq_send_files(
    channel_cfg: dict[str, Any],
    chat_id: str,
    files: list[str],
    is_group: bool = False,
    inter_file_delay_ms: int = 0,
) -> str | None:
    try:
        qq_send_files(
            channel_cfg,
            chat_id,
            files,
            is_group=is_group,
            inter_file_delay_ms=inter_file_delay_ms,
        )
    except Exception as exc:
        return str(exc)
    return None


def classify_delivery_warning(errors: list[str]) -> tuple[str, str]:
    combined = " ".join(str(item or "") for item in errors).lower()
    if "850012" in combined or "qq api" in combined or "upload api" in combined:
        return "qq-api-error", "qq_api_error"
    if any(str(item or "").lower().startswith("files:") for item in errors):
        return "artifact-delivery-warning", "artifact_send_failed"
    return "qq-delivery-warning", "qq_api_error"


def terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def terminate_admin_relay_for_task(task_id: str) -> None:
    admin_lock = read_json(ADMIN_LOCK_FILE, default={}) or {}
    if not isinstance(admin_lock, dict):
        return
    admin_task_id = str(admin_lock.get("task_id") or "").strip()
    try:
        admin_pid = int(admin_lock.get("pid") or 0)
    except (TypeError, ValueError):
        admin_pid = 0
    if admin_task_id == task_id and admin_pid > 0:
        terminate_process_tree(admin_pid)


def run_json_command(
    command: list[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    stop_context: tuple[str, str] | None = None,
) -> dict[str, Any]:
    stopped = False
    stop_payload: dict[str, Any] | None = None
    timed_out = False

    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        deadline = time.time() + timeout_seconds
        while process.poll() is None:
            if stop_context:
                session_key, session_id = stop_context
                stop_payload = read_stop_request(session_key, session_id)
                if stop_payload:
                    terminate_process_tree(process.pid)
                    stopped = True
                    break

            if time.time() >= deadline:
                terminate_process_tree(process.pid)
                timed_out = True
                break

            time.sleep(WORKER_POLL_SECONDS)

        if process.poll() is None:
            try:
                process.wait(timeout=15)
            except Exception:
                terminate_process_tree(process.pid)
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass

    stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
    stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
    returncode = process.returncode if process.returncode is not None else (-2 if stopped else -1)
    if timed_out:
        stderr = (stderr + f"\nTimed out after {timeout_seconds} seconds").strip()
        write_text(stderr_path, stderr)
    payload: dict[str, Any] = {
        "ok": returncode == 0 and not stopped and not timed_out,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "stopped": stopped,
        "stop_payload": stop_payload,
        "json": None,
    }
    if stdout.strip():
        try:
            payload["json"] = json.loads(stdout)
        except json.JSONDecodeError:
            payload["json"] = None
    return payload


def build_tool_env(temp_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    normalized_entries = {os.path.normcase(os.path.normpath(entry)) for entry in path_entries}

    preferred_entry = str(PREFERRED_CODEX_BIN_DIR)
    preferred_norm = os.path.normcase(os.path.normpath(preferred_entry))
    if PREFERRED_CODEX_BIN_DIR.exists() and preferred_norm not in normalized_entries:
        path_entries.insert(0, preferred_entry)

    temp_path = temp_root or TMP_DIR
    ensure_dir(temp_path)
    temp_root_str = str(temp_path)
    env["PATH"] = os.pathsep.join(path_entries)
    env["TMP"] = temp_root_str
    env["TEMP"] = temp_root_str
    env["TMPDIR"] = temp_root_str

    # Do not let the current Codex desktop thread/broker context leak into
    # child Codex runs that should behave like fresh external workers.
    for key in (
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_THREAD_ID",
        "CODEX_COMPANION_APP_SERVER_ENDPOINT",
    ):
        env.pop(key, None)

    return env


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw_text = str(text or "").strip()
    if not raw_text:
        return None

    candidates = [raw_text]
    for pattern in (r"```json\s*(\{.*?\})\s*```", r"```\s*(\{.*?\})\s*```"):
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE | re.DOTALL):
            candidates.append(match.group(1).strip())

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
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def build_shell_command_prefix(candidate: str) -> list[str]:
    suffix = Path(candidate).suffix.lower()
    if suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", candidate]
    return [candidate]


def find_codex_command_prefix() -> list[str]:
    launcher_candidates = [
        str(PREFERRED_CODEX_CMD) if PREFERRED_CODEX_CMD.exists() else "",
        str(PREFERRED_CODEX_PWSH) if PREFERRED_CODEX_PWSH.exists() else "",
        shutil.which("codex.cmd") or "",
        shutil.which("codex.ps1") or "",
        shutil.which("codex") or "",
    ]
    for candidate in launcher_candidates:
        if not candidate:
            continue
        command_prefix = build_shell_command_prefix(candidate)
        if command_is_invocable([*command_prefix, "--version"]):
            return command_prefix

    node_exe = shutil.which("node")
    js_candidates = [
        str(PREFERRED_CODEX_JS) if PREFERRED_CODEX_JS.exists() else "",
        str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"),
    ]
    if node_exe:
        for candidate in js_candidates:
            if not candidate or not Path(candidate).exists():
                continue
            command_prefix = [node_exe, candidate]
            if command_is_invocable([*command_prefix, "--version"]):
                return command_prefix

    raise Phase1Error("codex-missing", "Codex CLI not found. Install codex.cmd/codex.ps1 or restore the codex.js bundle.")


def build_plan_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "admin_required": {"type": "string", "enum": ["yes", "no", "maybe"]},
            "change_scope": {"type": "string", "enum": ["none", "project", "unknown"]},
            "goal": {"type": "string"},
            "deliverables": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 7},
            "risks": {"type": "array", "items": {"type": "string"}},
            "claude_primary": {"type": "array", "items": {"type": "string"}},
            "codex_review_only": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "admin_required",
            "change_scope",
            "goal",
            "deliverables",
            "steps",
            "risks",
            "claude_primary",
            "codex_review_only",
        ],
    }


def build_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "verdict": {"type": "string", "enum": ["pass", "needs-attention", "inconclusive"]},
                    "summary": {"type": "string"},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                                "title": {"type": "string"},
                                "file": {"type": "string"},
                                "line_start": {"type": "integer"},
                                "recommendation": {"type": "string"},
                            },
                            "required": ["severity", "title", "file", "line_start", "recommendation"],
                        },
                    },
                },
                "required": ["verdict", "summary", "findings"],
            }
        },
        "required": ["result"],
    }


def build_codex_config_args(reasoning_effort: str | None = None) -> list[str]:
    args: list[str] = []
    if reasoning_effort:
        args.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    return args


def build_codex_exec_command(
    prompt: str,
    project_cwd: Path,
    schema_path: Path,
    output_path: Path,
    reasoning_effort: str | None = None,
) -> list[str]:
    return [
        *find_codex_command_prefix(),
        "exec",
        *build_codex_config_args(reasoning_effort),
        "-C",
        str(project_cwd),
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-s",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        prompt,
    ]


def run_codex_exec_json(
    prompt: str,
    schema: dict[str, Any],
    task_dir: Path,
    project_cwd: Path,
    output_stem: str,
    timeout_seconds: int,
    reasoning_effort: str | None = None,
    stop_context: tuple[str, str] | None = None,
) -> dict[str, Any]:
    stdout_path = task_dir / f"{output_stem}.stdout.log"
    stderr_path = task_dir / f"{output_stem}.stderr.log"
    schema_path = task_dir / f"{output_stem}.schema.json"
    output_path = task_dir / f"{output_stem}.last-message.json"
    write_json(schema_path, schema)
    tool_env = build_tool_env(task_dir / "tool-tmp")
    command = build_codex_exec_command(prompt, project_cwd, schema_path, output_path, reasoning_effort=reasoning_effort)
    result = run_json_command(
        command,
        project_cwd,
        stdout_path,
        stderr_path,
        timeout_seconds,
        env=tool_env,
        stop_context=stop_context,
    )
    last_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    result["last_message"] = last_message
    result["json"] = extract_json_object(last_message)
    return result


def render_codex_plan_text(plan_payload: dict[str, Any]) -> str:
    def bullet_lines(title: str, items: list[str]) -> list[str]:
        lines = [title]
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- 无")
        lines.append("")
        return lines

    steps = [str(item).strip() for item in plan_payload.get("steps", []) if str(item).strip()]
    lines = [
        f"ADMIN_REQUIRED: {plan_payload.get('admin_required', 'maybe')}",
        f"CHANGE_SCOPE: {plan_payload.get('change_scope', 'unknown')}",
        "",
    ]
    lines.extend(bullet_lines("目标", [str(plan_payload.get("goal", "")).strip()]))
    lines.extend(bullet_lines("交付物", [str(item).strip() for item in plan_payload.get("deliverables", []) if str(item).strip()]))
    lines.append("建议执行步骤")
    if steps:
        lines.extend(f"{index}. {item}" for index, item in enumerate(steps, 1))
    else:
        lines.append("1. 无")
    lines.append("")
    lines.extend(bullet_lines("关键风险", [str(item).strip() for item in plan_payload.get("risks", []) if str(item).strip()]))
    lines.extend(
        bullet_lines(
            "哪些部分适合 Claude 主做",
            [str(item).strip() for item in plan_payload.get("claude_primary", []) if str(item).strip()],
        )
    )
    lines.extend(
        bullet_lines(
            "哪些部分只需要 Codex 审查",
            [str(item).strip() for item in plan_payload.get("codex_review_only", []) if str(item).strip()],
        )
    )
    return "\n".join(lines).strip()


def build_task_special_context(task: dict[str, Any], trusted_search_roots: list[str] | None = None) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    lines: list[str] = []

    history_context = str(phase1_metadata.get("history_context") or "").strip()
    if history_context:
        lines.append("额外历史项目上下文：")
        lines.append(history_context)

    if str(task.get("system_action") or "").strip() == "authorized_ai_file_search" or phase1_metadata.get("authorized_computer_search"):
        lines.append("这轮任务是来自已授权 QQ 用户的显式全机文件搜索请求。")
        roots = trusted_search_roots if trusted_search_roots is not None else phase1_metadata.get("computer_search_roots")
        if isinstance(roots, list) and roots:
            lines.append("允许搜索的本地根目录：")
            lines.extend(f"- {str(item).strip()}" for item in roots if str(item).strip())
        lines.append("你可以在这些根目录下读取、筛选、复制或打包候选文件，但不要修改不相关文件。")
        if phase1_metadata.get("computer_search_wants_send"):
            lines.append("如果找到符合条件的文件，可以把绝对路径写入 LAST_ARTIFACTS.json 的 files 字段，系统会尝试回传到 QQ。")
        else:
            lines.append("如果只找到候选文件但没有明确要求立即回传，请先汇报候选结果。")

    return "\n".join(line for line in lines if line).strip()


def build_codex_plan_prompt(
    project_id: str,
    project_root: str,
    session_id: str,
    user_request: str,
    attachments_text: str,
    special_context: str = "",
) -> str:
    return textwrap.dedent(
        f"""
        你现在只做一件事：对下面这次 Phase 1 远程开发任务做高判断力的只读拆解。

        要求：
        1. 不要写代码。
        2. 不要修改文件。
        3. 用中文回答。
        4. 输出结构：
           - ADMIN_REQUIRED: yes|no|maybe
           - CHANGE_SCOPE: none|project|unknown
           - 目标
           - 交付物
           - 建议执行步骤（最多 7 步）
           - 关键风险
           - 哪些部分适合 Claude 主做
           - 哪些部分只需要 Codex 审查
        5. ADMIN_REQUIRED 的判断标准：
           - yes：大概率需要管理员权限、系统级改动或 Windows 受保护资源
           - no：明显只需要项目级读写和普通命令
           - maybe：现在无法可靠判断
        6. CHANGE_SCOPE 的判断标准：
           - none：这轮更像回答、解释、总结、状态查询，不需要改项目文件
           - project：大概率需要改项目文件、运行测试、生成正式产物，或做交付级实现
           - unknown：现在无法可靠判断

        当前项目：{project_id}
        项目根目录：{project_root}
        当前会话：{session_id}
        附件情况：
        {attachments_text}

        额外上下文：
        {special_context or "无"}

        用户请求：
        {user_request.strip()}
        """
    ).strip()


def format_recent_context(events: list[dict[str, Any]]) -> str:
    if not events:
        return "无最近上下文。"
    lines = []
    for item in events[-8:]:
        kind = str(item.get("type") or "event")
        if kind == "user_enqueued":
            lines.append(f"- 用户补充：{truncate(str(item.get('request') or ''), 200)}")
        elif kind == "assistant_result":
            lines.append(f"- 上次结果：{truncate(str(item.get('summary') or ''), 200)}")
        elif kind == "task_status":
            lines.append(f"- 任务状态：{truncate(str(item.get('detail') or ''), 200)}")
    return "\n".join(lines) if lines else "无最近上下文。"


def build_batch_request(batch: list[dict[str, Any]]) -> str:
    if len(batch) == 1:
        return batch[0].get("user_request", "").strip()

    lines = ["下面这些内容来自同一手机会话里的连续补充，请按时间顺序合并理解：", ""]
    for index, task in enumerate(batch, 1):
        lines.append(f"{index}. [{task.get('received_at', '')}] {task.get('user_request', '').strip()}")
    return "\n".join(lines)


def gather_batch_attachments(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in batch:
        for item in task.get("attachments", []) or []:
            attachment_key = (
                str(item.get("path") or "").strip()
                or str(item.get("source_url") or "").strip()
            )
            if not attachment_key or attachment_key.lower() in seen:
                continue
            seen.add(attachment_key.lower())
            attachments.append(item)
    return attachments


def build_claude_prompt(
    task: dict[str, Any],
    batch: list[dict[str, Any]],
    codex_plan: str,
    recent_context_text: str,
    review_feedback: str | None = None,
    admin_escalation_file: Path | None = None,
    admin_mode: bool = False,
    trusted_search_roots: list[str] | None = None,
) -> str:
    attachments = gather_batch_attachments(batch)
    special_context = build_task_special_context(task, trusted_search_roots=trusted_search_roots)
    extra_review = ""
    if review_feedback:
        extra_review = textwrap.dedent(
            f"""
            额外上下文：Codex 对抗式审查给出了下面这些反馈，请优先核对并修正有效问题。
            {review_feedback.strip()}
            """
        ).strip()

    admin_rule = ""
    if admin_mode:
        admin_rule = "7. 当前这一轮已经运行在管理员 relay 中；如果确有必要，可以完成系统级步骤，但仍要保持最小必要改动。"
    elif admin_escalation_file is not None:
        admin_rule = textwrap.dedent(
            f"""
            7. 如果你确认当前任务需要管理员权限，请不要反复硬试或卡住。
               请立刻把下面这个 JSON 写入 `{admin_escalation_file}`：
               {{
                 "requires_admin": true,
                 "reason": "一句中文原因",
                 "next_action": "需要切换到管理员执行的步骤"
               }}
               然后停止继续执行高权限步骤，只输出一段简短中文说明。
            """
        ).strip()

    return textwrap.dedent(
        f"""
        你现在运行在项目 `{ROOT}` 的远程开发链路里。
        这轮任务真正要操作的项目根目录是 `{task.get("project_root", "")}`，默认就在这个目录里执行命令和改文件。
        上游用户来自 QQ，这一轮由你作为主执行者处理任务。

        必须遵守：
        1. 先明确验证方式，再开始改文件。
        2. 你是主执行者，不要把普通实现工作交给 Codex。
        3. 只有在非常困难且局部的问题上，才可以小范围再调用 Codex。
        4. 在任务开始、关键里程碑、任务结束前，更新 `{ACTIVE_TASK_FILE}` 和 `{QQ_PROGRESS_FILE}`。
           不要把这些运行时文件写到当前项目目录下的相对 `runtime/` 里。
        5. 如果生成了值得回传给 QQ 的文件或链接，请更新 `{ARTIFACTS_FILE}`：
           {{
              "files": ["绝对路径"],
              "urls": ["http://..."],
              "notes": ["一句简短说明"]
           }}
        6. 最终汇报必须用中文，适合手机 QQ 阅读，优先回答：结果、做了什么、怎么验证、还有什么风险。
        {admin_rule}

        当前任务信息：
        - task_id: {task.get("task_id", "")}
        - project_id: {task.get("project_id", "")}
        - project_root: {task.get("project_root", "")}
        - session_key: {task.get("session_key", "")}
        - session_id: {task.get("session_id", "")}
        - received_at: {task.get("received_at", "")}

        额外任务上下文：
        {special_context or "无"}

        最近会话上下文：
        {recent_context_text}

        本轮收到的用户输入：
        {build_batch_request(batch)}

        本轮附件：
        {summarize_attachments(attachments)}

        Codex 只读拆解结果：
        {codex_plan.strip() or "本轮没有可用的 Codex 规划结果。"}

        {extra_review}

        现在请你直接执行任务，必要时修改文件、运行命令并验证结果，然后给出最终中文汇报。
        """
    ).strip()


def build_review_focus() -> str:
    return (
        "请重点检查当前 working tree 是否存在会阻碍交付的错误、遗漏验证、潜在回归、"
        "状态管理问题、脚本不可靠点、路径或权限问题，以及任何不适合直接交付给手机端用户的风险。"
    )


def build_review_task_prompt() -> str:
    return textwrap.dedent(
        """
        Perform a read-only adversarial code review for the current git working tree of this repository.

        Rules:
        1. Do not modify files.
        2. Do not run destructive commands.
        3. Do not execute application entrypoints or reproductions that write into `runtime/`, `queue/`, `inbox/`,
           `admin-inbox/`, `tasks/`, or `sessions/`.
        4. Do not call `route_task`, `Launch-Phase1Task.ps1`, `Test-Phase1Pipeline.ps1`, or any script that can enqueue work.
        5. Inspect `git status --short`, `git diff --stat`, and the relevant diffs before judging.
        6. Focus on bugs, regressions, missing validation, state handling mistakes, path or permission mistakes,
           delivery failures, and anything that would make this unsafe to hand back to a mobile QQ user.
        7. Ignore clearly unrelated pre-existing changes when possible.
        8. Return strict JSON only, with no markdown fences and no extra commentary.

        Output schema:
        {
          "result": {
            "verdict": "pass" | "needs-attention" | "inconclusive",
            "summary": "short Chinese summary",
            "findings": [
              {
                "severity": "high" | "medium" | "low",
                "title": "short Chinese title",
                "file": "relative/path",
                "line_start": 1,
                "recommendation": "short Chinese recommendation"
              }
            ]
          }
        }

        Review focus:
        Check the current working tree for blocking bugs, missing validation, risky regressions,
        state management issues, unreliable scripts, path or permission mistakes, delivery failures,
        and anything unsafe to ship directly to a remote mobile QQ user.
        """
    ).strip()


def map_codex_review_priority(priority: str) -> str:
    normalized = str(priority or "").strip().upper()
    if normalized in {"P0", "P1"}:
        return "high"
    if normalized == "P2":
        return "medium"
    return "low"


def normalize_review_file_path(raw_path: str, project_cwd: Path) -> str:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return "unknown"
    try:
        path_obj = Path(path_text)
        if path_obj.is_absolute():
            try:
                return path_obj.relative_to(project_cwd).as_posix()
            except ValueError:
                return path_obj.as_posix()
    except Exception:
        pass
    return path_text.replace("\\", "/")


def parse_codex_review_text(review_text: str, project_cwd: Path) -> dict[str, Any] | None:
    raw_text = str(review_text or "").strip()
    if not raw_text:
        return None

    lines = [line.rstrip() for line in raw_text.splitlines()]
    findings: list[dict[str, Any]] = []
    summary_lines: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.lower() == "review comment:":
            index += 1
            break
        summary_lines.append(line)
        index += 1

    while index < len(lines):
        current = lines[index].rstrip()
        match = CODEx_REVIEW_FINDING_PATTERN.match(current.strip())
        if not match:
            index += 1
            continue

        priority, title, location = match.groups()
        location_match = CODEX_REVIEW_LOCATION_PATTERN.match(location.strip())
        file_name = "unknown"
        line_start = 1
        if location_match:
            file_name = normalize_review_file_path(location_match.group("path"), project_cwd)
            line_start = int(location_match.group("line"))
        else:
            file_name = normalize_review_file_path(location, project_cwd)

        recommendation_lines: list[str] = []
        index += 1
        while index < len(lines):
            extra_line = lines[index]
            if CODEx_REVIEW_FINDING_PATTERN.match(extra_line.strip()):
                break
            stripped = extra_line.strip()
            if stripped:
                recommendation_lines.append(stripped)
            index += 1

        findings.append(
            {
                "severity": map_codex_review_priority(priority),
                "title": title.strip(),
                "file": file_name,
                "line_start": line_start,
                "recommendation": " ".join(recommendation_lines).strip(),
            }
        )

    summary_text = " ".join(summary_lines).strip()
    if not summary_text:
        summary_text = "Codex 审查完成。"

    verdict = "needs-attention" if findings else "pass"
    return {
        "result": {
            "verdict": verdict,
            "summary": summary_text,
            "findings": findings,
        },
        "rawOutput": raw_text,
    }


def normalize_review_payload(wrapper_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(wrapper_payload, dict):
        return None

    if isinstance(wrapper_payload.get("result"), dict):
        return wrapper_payload

    raw_output = str(wrapper_payload.get("rawOutput") or "")
    parsed = extract_json_object(raw_output)
    if not isinstance(parsed, dict):
        return None

    result_payload = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed
    if not isinstance(result_payload, dict):
        return None

    return {
        "result": result_payload,
        "rawOutput": raw_output,
        "threadId": wrapper_payload.get("threadId"),
        "status": wrapper_payload.get("status"),
    }


def build_review_summary(review_json: dict[str, Any] | None) -> str:
    if not review_json:
        return ""
    result = review_json.get("result") or {}
    findings = result.get("findings") or []
    if not findings:
        return result.get("summary", "Codex 审查未发现阻断级问题。")
    lines = [result.get("summary", "Codex 审查发现了一些需要注意的问题。")]
    for finding in findings[:5]:
        severity = finding.get("severity", "unknown")
        title = finding.get("title", "未命名问题")
        file_name = finding.get("file", "unknown")
        line_start = finding.get("line_start", "?")
        recommendation = finding.get("recommendation", "")
        lines.append(f"- [{severity}] {title} ({file_name}:{line_start})")
        if recommendation:
            lines.append(f"  建议：{recommendation}")
    return "\n".join(lines)


def normalize_match_path(raw_path: str | Path) -> str:
    return str(raw_path or "").replace("/", "\\").lower()


def should_skip_review_snapshot_path(relative_path: Path) -> bool:
    parts = [part.lower() for part in relative_path.parts if part not in {"", "."}]
    if not parts:
        return False
    if parts[0] in REVIEW_SNAPSHOT_TOP_LEVEL_EXCLUDES:
        return True
    return any(part in REVIEW_SNAPSHOT_INNER_EXCLUDES for part in parts)


def run_capture_command(command: list[str], cwd: Path, timeout_seconds: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise Phase1Error("review-snapshot-failed", f"Failed to run {' '.join(command)}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise Phase1Error("review-snapshot-failed", f"{' '.join(command)} failed: {stderr or result.returncode}")
    return result


def git_has_head_commit(project_cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(project_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0


def overlay_review_snapshot(source_root: Path, snapshot_dir: Path) -> None:
    for current_root, dirnames, filenames in os.walk(source_root):
        current_path = Path(current_root)
        relative_root = current_path.relative_to(source_root)
        if relative_root == Path("."):
            relative_root = Path()

        dirnames[:] = [
            name
            for name in dirnames
            if not should_skip_review_snapshot_path(relative_root / name)
        ]

        for filename in filenames:
            relative_path = relative_root / filename
            if should_skip_review_snapshot_path(relative_path):
                continue
            source_path = current_path / filename
            target_path = snapshot_dir / relative_path
            ensure_dir(target_path.parent)
            shutil.copy2(source_path, target_path)


def list_deleted_git_paths(project_cwd: Path) -> list[Path]:
    deleted_result = run_capture_command(
        ["git", "diff", "--name-only", "--diff-filter=D", "HEAD"],
        project_cwd,
        timeout_seconds=30,
    )
    deleted_paths: list[Path] = []
    for line in deleted_result.stdout.splitlines():
        cleaned = line.strip()
        if cleaned:
            deleted_paths.append(Path(cleaned))
    return deleted_paths


def build_review_snapshot(task_dir: Path, project_cwd: Path) -> Path:
    snapshot_root = ensure_dir(TMP_DIR / "review-snapshots")
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{task_dir.name[-6:]}"
    snapshot_dir = snapshot_root / f"snapshot-{stamp}"
    archive_path = snapshot_root / f"baseline-{stamp}.zip"
    ensure_dir(snapshot_dir)

    try:
        run_capture_command(
            ["git", "archive", "--format=zip", "-o", str(archive_path), "HEAD"],
            project_cwd,
            timeout_seconds=90,
        )
        shutil.unpack_archive(str(archive_path), str(snapshot_dir))
        run_capture_command(["git", "init"], snapshot_dir, timeout_seconds=30)
        run_capture_command(["git", "config", "user.name", "Phase1 Review"], snapshot_dir, timeout_seconds=15)
        run_capture_command(["git", "config", "user.email", "phase1-review@example.local"], snapshot_dir, timeout_seconds=15)
        run_capture_command(["git", "add", "-A"], snapshot_dir, timeout_seconds=60)
        run_capture_command(["git", "commit", "-m", "phase1 review baseline"], snapshot_dir, timeout_seconds=60)
        overlay_review_snapshot(project_cwd, snapshot_dir)
        for deleted_path in list_deleted_git_paths(project_cwd):
            target_path = snapshot_dir / deleted_path
            if target_path.is_dir():
                shutil.rmtree(target_path, ignore_errors=True)
            else:
                target_path.unlink(missing_ok=True)
        return snapshot_dir
    finally:
        archive_path.unlink(missing_ok=True)


def build_plain_review_snapshot(task_dir: Path, project_cwd: Path) -> Path:
    snapshot_root = ensure_dir(TMP_DIR / "review-snapshots")
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{task_dir.name[-6:]}"
    snapshot_dir = snapshot_root / f"snapshot-{stamp}"
    ensure_dir(snapshot_dir)
    overlay_review_snapshot(project_cwd, snapshot_dir)
    return snapshot_dir


def remove_codex_project_trust_entry(project_path: Path) -> None:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return

    sanitize_codex_config(config_path, extra_snapshot_paths=[project_path])


def sanitize_codex_config(config_path: Path | None = None, extra_snapshot_paths: list[Path] | None = None) -> None:
    target_path = config_path or (Path.home() / ".codex" / "config.toml")
    if not target_path.exists():
        return

    raw_text = target_path.read_text(encoding="utf-8")
    lines = raw_text.splitlines(keepends=True)
    prelude: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_header: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current_header is None:
                prelude.extend(current_lines)
            else:
                sections.append((current_header, current_lines))
            current_header = stripped
            current_lines = [line]
            continue
        current_lines.append(line)

    if current_header is not None:
        sections.append((current_header, current_lines))
    elif not prelude:
        return

    normalized_snapshots = {
        normalize_match_path(path)
        for path in (extra_snapshot_paths or [])
        if str(path or "").strip()
    }
    stale_prefixes = (
        normalize_match_path(TMP_DIR / "review-snapshots") + "\\snapshot-",
        normalize_match_path(TMP_DIR / "mini-review-"),
    )

    updated: list[str] = list(prelude)
    changed = False

    for header, block_lines in sections:
        block_text = "".join(block_lines)
        lowered_header = header.lower()
        drop_block = False

        if lowered_header.startswith("[projects.'") and lowered_header.endswith("']"):
            project_path = lowered_header[len("[projects.'") : -2]
            project_path = project_path.replace("/", "\\")
            if project_path in normalized_snapshots:
                drop_block = True
            elif "\\snapshot-" in project_path or "\\mini-review-" in project_path:
                drop_block = True

        if drop_block:
            changed = True
            continue

        updated.extend(block_lines)

    if changed:
        target_path.write_text("".join(updated).rstrip() + "\n", encoding="utf-8")


def is_synthetic_review_artifact_task(task: dict[str, Any]) -> bool:
    source_task_file = normalize_match_path(str(task.get("source_task_file") or ""))
    session_key = str(task.get("session_key") or "").strip().lower()
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    synthetic_flag = bool(
        task.get("synthetic_review")
        or metadata.get("synthetic_review")
        or phase1_metadata.get("synthetic_review")
    )
    if not synthetic_flag:
        return False
    if not any(marker in source_task_file for marker in SYNTHETIC_REVIEW_SOURCE_MARKERS):
        return False
    return any(session_key.startswith(prefix) for prefix in SYNTHETIC_REVIEW_SESSION_PREFIXES)


def infer_plan_change_scope(plan_payload: dict[str, Any]) -> str:
    declared_scope = str(plan_payload.get("change_scope") or "").strip().lower()
    if declared_scope in {"none", "project", "unknown"}:
        return declared_scope

    fragments: list[str] = []
    for key in ("goal",):
        value = str(plan_payload.get(key) or "").strip()
        if value:
            fragments.append(value)
    for key in ("deliverables", "steps", "risks", "claude_primary", "codex_review_only"):
        raw_items = plan_payload.get(key, [])
        if isinstance(raw_items, list):
            fragments.extend(str(item).strip() for item in raw_items if str(item).strip())

    normalized_text = "\n".join(fragments).lower()
    none_markers = (
        "纯文本回复",
        "简短中文回复",
        "一句",
        "单句",
        "固定文案",
        "只需返回",
        "无需实现",
        "无需代码",
        "无需修改文件",
        "不涉及代码",
        "轻量任务",
        "reply",
        "one short chinese sentence",
    )
    if any(marker in normalized_text for marker in none_markers):
        return "none"

    project_markers = (
        "修改文件",
        "改文件",
        "实现",
        "修复",
        "重构",
        "编写",
        "新增",
        "脚本",
        "代码",
        "运行测试",
        "产物",
        "artifact",
        "patch",
        "edit file",
        "write code",
    )
    if any(marker in normalized_text for marker in project_markers):
        return "project"
    return "unknown"


def normalize_plan_payload(plan_payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(plan_payload)
    normalized["change_scope"] = infer_plan_change_scope(normalized)
    return normalized


def run_codex_plan(
    task: dict[str, Any],
    task_dir: Path,
    batch: list[dict[str, Any]],
    project_cwd: Path,
    trusted_search_roots: list[str] | None = None,
) -> dict[str, Any]:
    attachments_text = summarize_attachments(gather_batch_attachments(batch))
    result = run_codex_exec_json(
        build_codex_plan_prompt(
            project_id=str(task.get("project_id") or ""),
            project_root=str(task.get("project_root") or ""),
            session_id=str(task.get("session_id") or ""),
            user_request=build_batch_request(batch),
            attachments_text=attachments_text,
            special_context=build_task_special_context(task, trusted_search_roots=trusted_search_roots),
        ),
        build_plan_schema(),
        task_dir,
        project_cwd,
        "codex-plan",
        CODEX_PLAN_TIMEOUT_SECONDS,
        reasoning_effort=CODEX_PLAN_REASONING_EFFORT,
        stop_context=(str(task.get("session_key") or ""), str(task.get("session_id") or "")),
    )
    plan_payload = result.get("json")
    if isinstance(plan_payload, dict):
        normalized_plan = normalize_plan_payload(plan_payload)
        result["json"] = {
            "plan": normalized_plan,
            "rawOutput": render_codex_plan_text(normalized_plan),
        }
    else:
        result["json"] = None
    return result


def is_git_repository(project_cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def run_codex_review_subcommand(
    task_dir: Path,
    project_cwd: Path,
    stop_context: tuple[str, str] | None = None,
) -> dict[str, Any]:
    sanitize_codex_config()
    stdout_path = task_dir / "codex-review.stdout.log"
    stderr_path = task_dir / "codex-review.stderr.log"
    output_path = task_dir / "codex-review.last-message.txt"
    snapshot_info_path = task_dir / "codex-review.snapshot.json"
    snapshot_dir = build_review_snapshot(task_dir, project_cwd)
    write_json(
        snapshot_info_path,
        {
            "created_at": now_iso(),
            "source_project_root": str(project_cwd),
            "snapshot_dir": str(snapshot_dir),
            "cleanup": "pending",
        },
    )
    tool_env = build_tool_env(task_dir / "tool-tmp")
    command = [
        *find_codex_command_prefix(),
        "exec",
        *build_codex_config_args(CODEX_REVIEW_REASONING_EFFORT),
        "-C",
        str(snapshot_dir),
        "--ephemeral",
        "--color",
        "never",
        # `codex exec review` hangs on this Windows host when `-s read-only`
        # is supplied, even though the default review flow completes quickly.
        # Keep the dedicated review subcommand on its stable path here.
        "review",
        "--uncommitted",
        "-o",
        str(output_path),
    ]
    try:
        result = run_json_command(
            command,
            snapshot_dir,
            stdout_path,
            stderr_path,
            CODEX_REVIEW_TIMEOUT_SECONDS,
            env=tool_env,
            stop_context=stop_context,
        )
        last_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        result["last_message"] = last_message
        result["json"] = parse_codex_review_text(last_message, project_cwd)
        return result
    finally:
        write_json(
            snapshot_info_path,
            {
                "created_at": now_iso(),
                "source_project_root": str(project_cwd),
                "snapshot_dir": str(snapshot_dir),
                "cleanup": "done",
            },
        )
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        remove_codex_project_trust_entry(snapshot_dir)


def run_codex_exec_review_from_snapshot(
    task_dir: Path,
    project_cwd: Path,
    stop_context: tuple[str, str] | None = None,
) -> dict[str, Any]:
    snapshot_info_path = task_dir / "codex-review.snapshot.json"
    snapshot_dir = build_plain_review_snapshot(task_dir, project_cwd)
    write_json(
        snapshot_info_path,
        {
            "created_at": now_iso(),
            "source_project_root": str(project_cwd),
            "snapshot_dir": str(snapshot_dir),
            "cleanup": "pending",
            "mode": "exec-snapshot",
        },
    )
    try:
        result = run_codex_exec_json(
            build_review_task_prompt(),
            build_review_schema(),
            task_dir,
            snapshot_dir,
            "codex-review",
            CODEX_REVIEW_TIMEOUT_SECONDS,
            reasoning_effort=CODEX_REVIEW_REASONING_EFFORT,
            stop_context=stop_context,
        )
        result["json"] = normalize_review_payload(result.get("json"))
        return result
    finally:
        write_json(
            snapshot_info_path,
            {
                "created_at": now_iso(),
                "source_project_root": str(project_cwd),
                "snapshot_dir": str(snapshot_dir),
                "cleanup": "done",
                "mode": "exec-snapshot",
            },
        )
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        remove_codex_project_trust_entry(snapshot_dir)


def run_codex_review(
    task_dir: Path,
    project_cwd: Path,
    stop_context: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if is_git_repository(project_cwd) and git_has_head_commit(project_cwd):
        try:
            result = run_codex_review_subcommand(task_dir, project_cwd, stop_context=stop_context)
            normalized_payload = result.get("json") if isinstance(result.get("json"), dict) else None
        except Phase1Error as exc:
            result = run_codex_exec_review_from_snapshot(task_dir, project_cwd, stop_context=stop_context)
            result["stderr"] = f"{result.get('stderr', '').strip()}\nSnapshot review fallback: {exc}".strip()
            normalized_payload = result.get("json") if isinstance(result.get("json"), dict) else None
    else:
        result = run_codex_exec_review_from_snapshot(task_dir, project_cwd, stop_context=stop_context)
        normalized_payload = result.get("json") if isinstance(result.get("json"), dict) else None

    if result.get("ok") and not normalized_payload:
        normalized_payload = {
            "result": {
                "verdict": "inconclusive",
                "summary": "Codex 审查已完成，但没有返回结构化结论，建议按未完全审查处理。",
                "findings": [],
            },
            "rawOutput": str(result.get("last_message") or ""),
        }
    result["json"] = normalized_payload
    return result


def review_needs_attention(review_result: dict[str, Any]) -> bool:
    payload = review_result.get("json") or {}
    result = payload.get("result") or {}
    return result.get("verdict") == "needs-attention" and bool(result.get("findings"))


def extract_admin_decision(codex_plan_text: str) -> str:
    if not codex_plan_text.strip():
        return "maybe"
    match = ADMIN_DECISION_PATTERN.search(codex_plan_text)
    if not match:
        return "maybe"
    return match.group(1).lower()


def should_use_admin_path(task: dict[str, Any], codex_plan_text: str) -> tuple[bool, str]:
    decision = extract_admin_decision(codex_plan_text)
    if decision == "yes":
        return True, "codex_plan"
    return False, "codex_plan" if decision == "no" else "none"


def is_admin_authorized_origin(task: dict[str, Any], channel_cfg: dict[str, Any]) -> bool:
    raw_allow_from = channel_cfg.get("allowFrom", [])
    if isinstance(raw_allow_from, str):
        allow_from = [raw_allow_from]
    elif isinstance(raw_allow_from, list):
        allow_from = [str(item).strip() for item in raw_allow_from if str(item).strip()]
    else:
        allow_from = []

    if not allow_from:
        return False

    chat_id = str(task.get("chat_id") or "").strip()
    sender_id = str(task.get("sender_id") or "").strip()
    session_key = str(task.get("session_key") or "").strip()
    allowed_values = set(allow_from)
    allowed_values.update(f"qq:{item}" for item in allow_from if not str(item).startswith("qq:"))
    return chat_id in allowed_values or sender_id in allowed_values or session_key in allowed_values


def build_admin_request_payload(
    task: dict[str, Any],
    batch: list[dict[str, Any]],
    codex_plan_text: str,
    task_dir: Path,
    reason: str,
    trigger_note: str,
) -> dict[str, Any]:
    return {
        "task_id": str(task.get("task_id") or task_dir.name),
        "received_at": task.get("received_at", ""),
        "user_request": build_batch_request(batch),
        "attachments": gather_batch_attachments(batch),
        "attachments_summary": summarize_attachments(gather_batch_attachments(batch)),
        "chat_id": task.get("chat_id", ""),
        "project_id": task.get("project_id", ""),
        "project_root": task.get("project_root", ""),
        "session_key": task.get("session_key", ""),
        "session_id": task.get("session_id", ""),
        "codex_plan": codex_plan_text,
        "admin_reason": reason,
        "trigger_note": trigger_note,
        "source_task_dir": str(task_dir),
    }


def enqueue_admin_request(task_id: str, payload: dict[str, Any]) -> Path:
    ensure_dir(ADMIN_INBOX_DIR)
    ensure_dir(ADMIN_TASKS_DIR)
    request_path = ADMIN_INBOX_DIR / f"{task_id}.json"
    write_json(request_path, payload)
    return request_path


def start_admin_relay() -> None:
    if not ADMIN_RELAY_TRIGGER.exists():
        raise Phase1Error("admin-relay-missing", f"Admin relay trigger not found: {ADMIN_RELAY_TRIGGER}")

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ADMIN_RELAY_TRIGGER),
            "-TaskName",
            "Phase1AdminRelay",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        hint = (
            "管理员中继计划任务不可用。请在管理员 PowerShell 里运行 "
            f"{ROOT / 'scripts' / 'Register-Phase1AdminRelayTask.ps1'}"
        )
        raise Phase1Error("admin-relay-launch", (result.stderr or result.stdout or hint).strip() or hint)


def wait_for_admin_relay(
    task_id: str,
    channel_cfg: dict[str, Any],
    chat_id: str,
    is_group: bool,
    heartbeat_seconds: int,
    session_key: str,
    session_id: str,
    parent_task_dir: Path,
) -> dict[str, Any]:
    relay_task_dir = ADMIN_TASKS_DIR / task_id
    status_path = relay_task_dir / "status.json"
    stdout_path = relay_task_dir / "claude.stdout.log"
    stderr_path = relay_task_dir / "claude.stderr.log"
    deadline = time.time() + ADMIN_RELAY_TIMEOUT_SECONDS
    last_mtime = QQ_PROGRESS_FILE.stat().st_mtime if QQ_PROGRESS_FILE.exists() else 0.0
    last_heartbeat = time.time()
    stop_notice_sent = False

    while time.time() < deadline:
        if status_path.exists():
            status = read_json(status_path, default={}) or {}
            phase = str(status.get("phase", "")).strip()
            if phase in {"finished", "failed", "stopped"}:
                return {
                    "phase": phase,
                    "status": status,
                    "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                    "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                    "result_text": str(status.get("result", "")).strip(),
                    "stop_payload": status.get("stop_payload") if isinstance(status, dict) else None,
                }

        time.sleep(WORKER_POLL_SECONDS)
        now = time.time()
        stop_payload = read_stop_request(session_key, session_id)
        if stop_payload and not stop_notice_sent:
            stop_notice_sent = True
            append_task_log(parent_task_dir, "admin_relay_stop_requested", reason=stop_payload.get("reason", ""))
            try_qq_send_text(channel_cfg, chat_id, "已收到停止请求，正在终止管理员执行通道。", is_group=is_group)
        if stop_payload:
            admin_lock = read_json(ADMIN_LOCK_FILE, default={}) or {}
            admin_task_id = str(admin_lock.get("task_id") or "").strip()
            admin_active = admin_task_id == task_id and is_pid_alive(admin_lock.get("pid"))
            if not admin_active:
                stop_message = f"管理员执行通道已按请求停止：{stop_payload.get('reason', '未提供原因')}"
                for pending_path in (
                    ADMIN_INBOX_DIR / f"{task_id}.json",
                    relay_task_dir / "request.json",
                ):
                    pending_path.unlink(missing_ok=True)
                status_payload = {
                    "task_id": task_id,
                    "phase": "stopped",
                    "finished_at": now_iso(),
                    "result": stop_message,
                    "stop_payload": stop_payload,
                }
                write_json(status_path, status_payload)
                return {
                    "phase": "stopped",
                    "status": status_payload,
                    "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                    "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                    "result_text": stop_message,
                    "stop_payload": stop_payload,
                }
        current_mtime = QQ_PROGRESS_FILE.stat().st_mtime if QQ_PROGRESS_FILE.exists() else 0.0
        if current_mtime > last_mtime:
            last_mtime = current_mtime
            last_heartbeat = now
        elif now - last_heartbeat >= heartbeat_seconds:
            last_heartbeat = now
            progress = QQ_PROGRESS_FILE.read_text(encoding="utf-8").strip() if QQ_PROGRESS_FILE.exists() else "管理员执行通道还在运行。"
            try_qq_send_text(channel_cfg, chat_id, progress, is_group=is_group)

    append_task_log(parent_task_dir, "admin_relay_timeout", task_id=task_id)
    terminate_admin_relay_for_task(task_id)
    raise Phase1Error("admin-relay-timeout", "管理员中继长时间没有产出最终状态，已请求终止该进程。")


def run_admin_relay(
    task: dict[str, Any],
    batch: list[dict[str, Any]],
    task_dir: Path,
    codex_plan_text: str,
    reason: str,
    trigger_note: str,
    channel_cfg: dict[str, Any],
    chat_id: str,
    is_group: bool,
    heartbeat_seconds: int,
) -> dict[str, Any]:
    if not is_admin_authorized_origin(task, channel_cfg):
        raise Phase1Error("admin-relay-unauthorized", "当前来源没有被授权进入管理员执行通道。")

    task_id = str(task.get("task_id") or task_dir.name)
    admin_task_dir = ADMIN_TASKS_DIR / task_id
    for stale_file in (
        admin_task_dir / "status.json",
        admin_task_dir / "claude.stdout.log",
        admin_task_dir / "claude.stderr.log",
        admin_task_dir / "request.json",
    ):
        stale_file.unlink(missing_ok=True)
    payload = build_admin_request_payload(task, batch, codex_plan_text, task_dir, reason, trigger_note)
    enqueue_admin_request(task_id, payload)
    start_admin_relay()
    return wait_for_admin_relay(
        task_id,
        channel_cfg,
        chat_id,
        is_group,
        heartbeat_seconds,
        str(task.get("session_key") or ""),
        str(task.get("session_id") or ""),
        task_dir,
    )


def write_worker_lock(payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_iso()
    write_json(WORKER_LOCK_FILE, payload)


def cleanup_worker_lock() -> None:
    payload = read_json(WORKER_LOCK_FILE, default={}) or {}
    try:
        owner_pid = int(payload.get("pid", -1))
    except (TypeError, ValueError):
        owner_pid = -1
    if owner_pid == os.getpid():
        WORKER_LOCK_FILE.unlink(missing_ok=True)


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


def append_task_log(task_dir: Path, event: str, **fields: Any) -> None:
    append_jsonl(task_dir / "task.jsonl", {"ts": now_iso(), "event": event, **fields})


def snapshot_runtime(task_dir: Path) -> None:
    for source in [ACTIVE_TASK_FILE, QQ_PROGRESS_FILE, ARTIFACTS_FILE]:
        if source.exists():
            target = task_dir / source.name
            write_text(target, source.read_text(encoding="utf-8"))


def save_status(task_dir: Path, payload: dict[str, Any]) -> None:
    write_json(task_dir / "status.json", payload)


def build_worker_status_payload(
    *,
    task_id: str,
    phase: str,
    task_name: str,
    project_id: str,
    project_root: str,
    session_key: str,
    session_id: str,
    started_at: str,
    chat_id: str = "",
    finished_at: str = "",
    result: str = "",
    error: str = "",
    error_type: str = "",
    failure_category: str = "",
    reply_code: str = "",
    user_visible_status: str = "",
    ack: str = "",
    message: str = "",
    batch_size: int | None = None,
    attachments: list[dict[str, Any]] | None = None,
    artifacts: dict[str, list[str]] | None = None,
    delivery_errors: list[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_failure_category = failure_category or failure_category_from_code(error_type)
    resolved_ack = ack or (reply_code or phase or "running")
    resolved_reply_code = reply_code or resolved_ack
    resolved_status = user_visible_status or (
        "stopped"
        if resolved_failure_category == "stopped"
        else "failed"
        if resolved_failure_category
        else "completed"
        if phase == "finished"
        else "running"
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
        meta={
            **(extra_meta or {}),
            "delivery_errors": delivery_errors or [],
        },
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
    if chat_id:
        payload["chat_id"] = chat_id
    if finished_at:
        payload["finished_at"] = finished_at
    if result:
        payload["result"] = result
    if error:
        payload["error"] = error
    if error_type:
        payload["error_type"] = error_type
    if batch_size is not None:
        payload["batch_size"] = batch_size
    if attachments is not None:
        payload["attachments"] = attachments
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if delivery_errors:
        payload["delivery_errors"] = delivery_errors
    if extra_meta:
        payload["meta"] = extra_meta
    return payload_with_receipt(payload, receipt)


def build_user_failure_message(exc: Phase1Error) -> str:
    failure_category = failure_category_from_code(exc.category)
    if failure_category == "stopped":
        return f"任务已停止：{exc}"
    if failure_category == "unauthorized_sender":
        return f"当前来源没有被授权执行这个操作：{exc}"
    if failure_category == "gateway_unavailable":
        return f"QQ 通道当前不可用，请稍后再试：{exc}"
    if failure_category == "environment_invalid":
        return f"当前运行环境还没有准备好：{exc}"
    if failure_category == "artifact_send_failed":
        return f"任务主体已完成，但结果文件回传失败：{exc}"
    if failure_category == "timeout":
        return f"任务执行超时：{exc}"
    if failure_category == "admin_relay_failed":
        return f"管理员执行通道失败：{exc}"
    return f"任务执行失败：{exc}"


def artifact_extra_roots_for_task(task: dict[str, Any], trusted_search_roots: list[str] | None = None) -> list[str]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    if str(task.get("system_action") or "").strip() == "authorized_ai_file_search" or phase1_metadata.get("authorized_computer_search"):
        roots = trusted_search_roots if trusted_search_roots is not None else phase1_metadata.get("computer_search_roots")
        if isinstance(roots, list):
            return [str(item).strip() for item in roots if str(item).strip()]
    return []


def is_health_probe_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    return str(task.get("system_action") or "").strip() == "health_probe" or bool(phase1_metadata.get("health_probe"))


def execute_explicit_file_send(
    seed_task: dict[str, Any],
    config: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[Path, str]:
    payload = seed_task.get("system_payload") if isinstance(seed_task.get("system_payload"), dict) else {}
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise Phase1Error("local-file-send-invalid", "没有收到可回传的本地文件路径。")

    path = Path(raw_path)
    _, project_root, _, default_project_root = resolve_task_project_context(seed_task, config, settings)
    allowed_roots = build_local_file_access_roots(
        config=config,
        settings=settings,
        project_root=project_root,
        default_project_root=default_project_root,
    )
    if allowed_roots and not is_path_within_any_root(path, allowed_roots):
        raise Phase1Error("local-file-send-outside-scope", f"鐩爣鏂囦欢涓嶅湪褰撳墠鎺堟潈鑼冨洿鍐咃細{path}")
    if not path.exists():
        raise Phase1Error("local-file-send-missing", f"要回传的文件不存在：{path}")
    if not path.is_file():
        raise Phase1Error("local-file-send-invalid", f"目标不是文件：{path}")

    max_send_bytes = int(settings["artifacts"].get("maxTotalBytes") or 80 * 1024 * 1024)
    file_size = path.stat().st_size
    if file_size > max_send_bytes:
        raise Phase1Error(
            "local-file-send-too-large",
            f"文件大小 {format_file_size(file_size)}，超过当前直发上限 {format_file_size(max_send_bytes)}。",
        )
    if not qq_upload_size_allowed(file_size, settings):
        raise Phase1Error(
            "local-file-send-too-large",
            f"文件大小 {format_file_size(file_size)} 经 QQ 直传编码后会超出网关限制，当前不能直接发送：{path.name}",
        )

    message = f"已按你的显式指令回传本地文件：{path.name}（{format_file_size(file_size)}）。"
    return path, message


def handle_health_probe_task(
    seed_task: dict[str, Any],
    processing_path: Path,
    config: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    heartbeat_seconds = int(settings["heartbeat"].get("intervalSeconds") or 30 * 60)
    channel_cfg = config["channels"]["qq"]
    chat_id = str(seed_task.get("chat_id") or "").strip()
    is_group = is_group_task(seed_task)
    task_id = str(seed_task.get("task_id") or new_task_id()).strip()
    task_dir = TASKS_DIR / task_id
    ensure_dir(task_dir)

    metadata = seed_task.get("metadata") if isinstance(seed_task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    probe_source = str(phase1_metadata.get("health_probe_source") or "Test-Phase1Pipeline.ps1").strip() or "Test-Phase1Pipeline.ps1"
    probe_requested_at = str(phase1_metadata.get("health_probe_requested_at") or seed_task.get("received_at") or "").strip()

    task_name = "Phase 1 健康探针"
    session_key = str(seed_task.get("session_key") or "").strip()
    session_id = str(seed_task.get("session_id") or "").strip()
    project_id, project_root, project_cwd, _ = resolve_task_project_context(seed_task, config, settings)
    seed_task["project_id"] = project_id
    seed_task["project_root"] = project_root
    task_started_at = now_iso()

    reset_artifacts()
    write_json(task_dir / "task.json", seed_task)
    archive_claimed_queue_file(processing_path, task_dir)
    append_task_log(task_dir, "task_claimed", queue_depth=queue_depth(), batch_size=1, health_probe=True)

    bind_running_task_state(
        session_key=session_key,
        chat_id=chat_id,
        channel=str(seed_task.get("channel") or "qq"),
        default_project_id=project_id,
        default_project_root=project_root,
        project_id=project_id,
        project_root=project_root,
        session_id=session_id,
        task_id=task_id,
    )

    write_worker_lock(
        {
            "pid": os.getpid(),
            "phase": "health-probe",
            "active_task_id": task_id,
            "project_id": project_id,
            "session_key": session_key,
            "session_id": session_id,
            "worker_stdout_log": str(RUNTIME_DIR / "worker.out.log"),
            "worker_stderr_log": str(RUNTIME_DIR / "worker.err.log"),
            "started_at": task_started_at,
        }
    )

    update_runtime_state(
        task_name=task_name,
        status="running",
        progress=format_progress("健康探针", task_name, "正在执行秒级通道自检。"),
        owner="Claude Code",
        project_id=project_id,
        session_id=session_id,
        heartbeat_interval_seconds=heartbeat_seconds,
        started_at=task_started_at,
    )
    save_status(
        task_dir,
        build_worker_status_payload(
            task_id=task_id,
            phase="health-probe",
            task_name=task_name,
            chat_id=chat_id,
            project_id=project_id,
            project_root=str(project_cwd),
            session_key=session_key,
            session_id=session_id,
            started_at=task_started_at,
            ack="running",
            reply_code="health_probe_running",
            user_visible_status="running",
            message="已收到健康探针，正在快速检查通道状态。",
            extra_meta={
                "health_probe": True,
                "health_probe_source": probe_source,
                "health_probe_requested_at": probe_requested_at,
            },
        ),
    )

    try:
        queued_stop_request = read_stop_request(session_key, session_id)
        if queued_stop_request:
            raise Phase1Error("user-stop", f"用户在任务启动前请求停止：{queued_stop_request.get('reason', '未提供原因')}")

        pending_queue_depth = queue_depth()
        final_message = f"Phase 1 健康探针通过：router/worker 正常，当前待处理队列 {pending_queue_depth}。"
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "finished",
            format_progress("完成", task_name, final_message),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="finished",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                result=final_message,
                ack="finished",
                reply_code="completed",
                user_visible_status="completed",
                message=final_message,
                extra_meta={
                    "health_probe": True,
                    "health_probe_source": probe_source,
                    "health_probe_requested_at": probe_requested_at,
                    "pending_queue_depth": pending_queue_depth,
                },
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "finished",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=final_message,
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
                    "ts": now_iso(),
                    "type": "assistant_result",
                    "task_id": task_id,
                    "summary": final_message,
                    "health_probe": True,
                },
                build_receipt(
                    stage="worker",
                    ack="finished",
                    message=final_message,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    phase="finished",
                    reply_code="completed",
                    user_visible_status="completed",
                    meta={
                        "health_probe": True,
                        "health_probe_source": probe_source,
                        "pending_queue_depth": pending_queue_depth,
                    },
                ),
            ),
        )
        append_task_log(
            task_dir,
            "health_probe_finished",
            probe_source=probe_source,
            pending_queue_depth=pending_queue_depth,
        )
        text_error = try_qq_send_text(channel_cfg, chat_id, final_message, is_group=is_group)
        if text_error:
            delivery_errors = [f"text: {text_error}"]
            warning_error_type, warning_failure_category = classify_delivery_warning(delivery_errors)
            warning_message = f"{final_message}\n\nQQ 回执告警：{'; '.join(delivery_errors)}"
            save_status(
                task_dir,
                build_worker_status_payload(
                    task_id=task_id,
                    phase="finished",
                    task_name=task_name,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    session_key=session_key,
                    session_id=session_id,
                    started_at=task_started_at,
                    finished_at=finished_at,
                    result=warning_message,
                    delivery_errors=delivery_errors,
                    ack="finished",
                    reply_code="completed_with_delivery_warning",
                    user_visible_status="completed",
                    message=warning_message,
                    error="; ".join(delivery_errors),
                    error_type=warning_error_type,
                    failure_category=warning_failure_category,
                    extra_meta={
                        "health_probe": True,
                        "health_probe_source": probe_source,
                        "health_probe_requested_at": probe_requested_at,
                        "pending_queue_depth": pending_queue_depth,
                    },
                ),
            )
            merge_task_outcome_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=str(seed_task.get("channel") or "qq"),
                default_project_id=project_id,
                default_project_root=project_root,
                project_id=project_id,
                project_root=project_root,
                session_id=session_id,
                task_id=task_id,
                result_text=warning_message,
                progress="finished",
                reply_code="completed_with_delivery_warning",
                failure_category=warning_failure_category,
                finished_at=finished_at,
            )
            append_session_event(
                session_key,
                session_id,
                payload_with_receipt(
                    {
                        "ts": now_iso(),
                        "type": "task_status",
                        "task_id": task_id,
                        "detail": warning_message,
                        "delivery_errors": delivery_errors,
                        "health_probe": True,
                    },
                    build_receipt(
                        stage="worker",
                        ack="finished",
                        message=warning_message,
                        task_id=task_id,
                        session_key=session_key,
                        session_id=session_id,
                        project_id=project_id,
                        project_root=str(project_cwd),
                        phase="finished",
                        reply_code="completed_with_delivery_warning",
                        user_visible_status="completed",
                        failure_category=warning_failure_category,
                        error_code=warning_error_type,
                        error_message="; ".join(delivery_errors),
                        meta={
                            "health_probe": True,
                            "health_probe_source": probe_source,
                            "pending_queue_depth": pending_queue_depth,
                            "delivery_errors": delivery_errors,
                        },
                    ),
                ),
            )
            append_task_log(task_dir, "task_delivery_warning", errors=delivery_errors, health_probe=True)
    except Phase1Error as exc:
        error_message = build_user_failure_message(exc)
        failure_category = failure_category_from_code(exc.category)
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "failed",
            format_progress("失败", task_name, f"{exc.category}: {exc}"),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type=exc.category,
                ack="stopped" if failure_category == "stopped" else "failed",
                reply_code=exc.category,
                user_visible_status="stopped" if failure_category == "stopped" else "failed",
                message=error_message,
                extra_meta={
                    "health_probe": True,
                    "health_probe_source": probe_source,
                    "health_probe_requested_at": probe_requested_at,
                },
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "failed",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=error_message,
            progress="failed",
            reply_code=exc.category,
            failure_category=failure_category,
            finished_at=finished_at,
        )
        append_session_event(
            session_key,
            session_id,
            payload_with_receipt(
                {
                    "ts": now_iso(),
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": error_message,
                    "error_type": exc.category,
                    "health_probe": True,
                },
                build_receipt(
                    stage="worker",
                    ack="stopped" if failure_category == "stopped" else "failed",
                    message=error_message,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    phase="failed",
                    reply_code=exc.category,
                    user_visible_status="stopped" if failure_category == "stopped" else "failed",
                    failure_category=failure_category,
                    error_code=exc.category,
                    error_message=str(exc),
                    meta={
                        "health_probe": True,
                        "health_probe_source": probe_source,
                        "health_probe_requested_at": probe_requested_at,
                    },
                ),
            ),
        )
        append_task_log(task_dir, "task_failed", error_type=exc.category, error=str(exc), health_probe=True)
        notify_error = try_qq_send_text(channel_cfg, chat_id, error_message, is_group=is_group)
        if notify_error:
            append_task_log(task_dir, "task_failure_delivery_warning", errors=[f"text: {notify_error}"], health_probe=True)
    except Exception as exc:  # pragma: no cover
        error_message = f"任务执行失败：{exc}"
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "failed",
            format_progress("失败", task_name, error_message),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type="worker-unhandled",
                ack="failed",
                reply_code="worker-unhandled",
                user_visible_status="failed",
                message=error_message,
                extra_meta={
                    "health_probe": True,
                    "health_probe_source": probe_source,
                    "health_probe_requested_at": probe_requested_at,
                },
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "failed",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=error_message,
            progress="failed",
            reply_code="worker-unhandled",
            failure_category="worker_failed",
            finished_at=finished_at,
        )
        append_session_event(
            session_key,
            session_id,
            payload_with_receipt(
                {
                    "ts": now_iso(),
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": error_message,
                    "error_type": "worker-unhandled",
                    "health_probe": True,
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
                    reply_code="worker-unhandled",
                    user_visible_status="failed",
                    failure_category="worker_failed",
                    error_code="worker-unhandled",
                    error_message=str(exc),
                    meta={
                        "health_probe": True,
                        "health_probe_source": probe_source,
                        "health_probe_requested_at": probe_requested_at,
                    },
                ),
            ),
        )
        append_task_log(task_dir, "task_failed", error_type="worker-unhandled", error=str(exc), health_probe=True)
        notify_error = try_qq_send_text(channel_cfg, chat_id, error_message, is_group=is_group)
        if notify_error:
            append_task_log(task_dir, "task_failure_delivery_warning", errors=[f"text: {notify_error}"], health_probe=True)
    finally:
        clear_stop_request(session_key, session_id)


def handle_send_local_file_task(
    seed_task: dict[str, Any],
    processing_path: Path,
    config: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    heartbeat_seconds = int(settings["heartbeat"].get("intervalSeconds") or 30 * 60)
    inter_file_delay_ms = int(settings["artifacts"].get("interFileDelayMs") or 0)
    channel_cfg = config["channels"]["qq"]
    chat_id = str(seed_task.get("chat_id") or "").strip()
    is_group = is_group_task(seed_task)
    task_id = str(seed_task.get("task_id") or new_task_id()).strip()
    task_dir = TASKS_DIR / task_id
    ensure_dir(task_dir)

    task_name = truncate(seed_task.get("user_request", "发送本地文件到手机"), limit=120)
    session_key = str(seed_task.get("session_key") or "").strip()
    session_id = str(seed_task.get("session_id") or "").strip()
    project_id, project_root, project_cwd, _ = resolve_task_project_context(seed_task, config, settings)
    seed_task["project_id"] = project_id
    seed_task["project_root"] = project_root
    task_started_at = now_iso()

    reset_artifacts()
    write_json(task_dir / "task.json", seed_task)
    archive_claimed_queue_file(processing_path, task_dir)
    append_task_log(task_dir, "task_claimed", queue_depth=queue_depth(), batch_size=1)

    bind_running_task_state(
        session_key=session_key,
        chat_id=chat_id,
        channel=str(seed_task.get("channel") or "qq"),
        default_project_id=project_id,
        default_project_root=project_root,
        project_id=project_id,
        project_root=project_root,
        session_id=session_id,
        task_id=task_id,
    )

    write_worker_lock(
        {
            "pid": os.getpid(),
            "phase": "sending-local-file",
            "active_task_id": task_id,
            "project_id": project_id,
            "session_key": session_key,
            "session_id": session_id,
            "worker_stdout_log": str(RUNTIME_DIR / "worker.out.log"),
            "worker_stderr_log": str(RUNTIME_DIR / "worker.err.log"),
            "started_at": task_started_at,
        }
    )

    update_runtime_state(
        task_name=task_name,
        status="running",
        progress=format_progress("发送文件", task_name, "正在把你指定的本地文件回传到 QQ。"),
        owner="Claude Code",
        project_id=project_id,
        session_id=session_id,
        heartbeat_interval_seconds=heartbeat_seconds,
        started_at=task_started_at,
    )
    save_status(
        task_dir,
        build_worker_status_payload(
            task_id=task_id,
            phase="sending-local-file",
            task_name=task_name,
            chat_id=chat_id,
            project_id=project_id,
            project_root=str(project_cwd),
            session_key=session_key,
            session_id=session_id,
            started_at=task_started_at,
            ack="running",
            reply_code="sending_local_file",
            user_visible_status="running",
            message="已收到文件回传请求，正在发送到 QQ。",
        ),
    )
    try_qq_send_text(channel_cfg, chat_id, "已收到文件回传请求，正在发送到 QQ。", is_group=is_group)

    try:
        queued_stop_request = read_stop_request(session_key, session_id)
        if queued_stop_request:
            raise Phase1Error("user-stop", f"用户在任务启动前请求停止：{queued_stop_request.get('reason', '未提供原因')}")

        file_path, final_message = execute_explicit_file_send(seed_task, config, settings)
        append_task_log(task_dir, "explicit_file_send_ready", path=str(file_path))
        delivery_error = try_qq_send_files(
            channel_cfg,
            chat_id,
            [str(file_path)],
            is_group=is_group,
            inter_file_delay_ms=inter_file_delay_ms,
        )
        if delivery_error:
            raise Phase1Error("local-file-delivery", f"发送文件到 QQ 失败：{delivery_error}")

        artifacts = {"files": [str(file_path)], "urls": [], "notes": []}
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "finished",
            format_progress("完成", task_name, "本地文件已经回传到 QQ。"),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="finished",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                result=final_message,
                artifacts=artifacts,
                ack="finished",
                reply_code="completed",
                user_visible_status="completed",
                message=final_message,
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "finished",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=final_message,
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
                    "ts": now_iso(),
                    "type": "assistant_result",
                    "task_id": task_id,
                    "summary": final_message,
                    "artifacts": artifacts,
                },
                build_receipt(
                    stage="worker",
                    ack="finished",
                    message=final_message,
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
        append_task_log(task_dir, "task_finished", artifact_files=artifacts["files"])
        text_error = try_qq_send_text(channel_cfg, chat_id, final_message, is_group=is_group)
        if text_error:
            delivery_errors = [f"text: {text_error}"]
            warning_error_type, warning_failure_category = classify_delivery_warning(delivery_errors)
            warning_message = f"{final_message}\n\nQQ 回执告警：{'; '.join(delivery_errors)}"
            save_status(
                task_dir,
                build_worker_status_payload(
                    task_id=task_id,
                    phase="finished",
                    task_name=task_name,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    session_key=session_key,
                    session_id=session_id,
                    started_at=task_started_at,
                    finished_at=finished_at,
                    result=warning_message,
                    artifacts=artifacts,
                    delivery_errors=delivery_errors,
                    ack="finished",
                    reply_code="completed_with_delivery_warning",
                    user_visible_status="completed",
                    message=warning_message,
                    error="; ".join(delivery_errors),
                    error_type=warning_error_type,
                    failure_category=warning_failure_category,
                ),
            )
            merge_task_outcome_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=str(seed_task.get("channel") or "qq"),
                default_project_id=project_id,
                default_project_root=project_root,
                project_id=project_id,
                project_root=project_root,
                session_id=session_id,
                task_id=task_id,
                result_text=warning_message,
                progress="finished",
                reply_code="completed_with_delivery_warning",
                failure_category=warning_failure_category,
                finished_at=finished_at,
            )
            append_session_event(
                session_key,
                session_id,
                payload_with_receipt(
                    {
                        "ts": now_iso(),
                        "type": "task_status",
                        "task_id": task_id,
                        "detail": warning_message,
                        "delivery_errors": delivery_errors,
                    },
                    build_receipt(
                        stage="worker",
                        ack="finished",
                        message=warning_message,
                        task_id=task_id,
                        session_key=session_key,
                        session_id=session_id,
                        project_id=project_id,
                        project_root=str(project_cwd),
                        phase="finished",
                        reply_code="completed_with_delivery_warning",
                        user_visible_status="completed",
                        failure_category=warning_failure_category,
                        error_code=warning_error_type,
                        error_message="; ".join(delivery_errors),
                        meta={"delivery_errors": delivery_errors},
                    ),
                ),
            )
            append_task_log(task_dir, "task_delivery_warning", errors=delivery_errors)
    except Phase1Error as exc:
        error_message = build_user_failure_message(exc)
        failure_category = failure_category_from_code(exc.category)
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "failed",
            format_progress("失败", task_name, f"{exc.category}: {exc}"),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type=exc.category,
                ack="stopped" if failure_category == "stopped" else "failed",
                reply_code=exc.category,
                user_visible_status="stopped" if failure_category == "stopped" else "failed",
                message=error_message,
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "failed",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=error_message,
            progress="failed",
            reply_code=exc.category,
            failure_category=failure_category,
            finished_at=finished_at,
        )
        append_session_event(
            session_key,
            session_id,
            payload_with_receipt(
                {
                    "ts": now_iso(),
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": error_message,
                    "error_type": exc.category,
                },
                build_receipt(
                    stage="worker",
                    ack="stopped" if failure_category == "stopped" else "failed",
                    message=error_message,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    phase="failed",
                    reply_code=exc.category,
                    user_visible_status="stopped" if failure_category == "stopped" else "failed",
                    failure_category=failure_category,
                    error_code=exc.category,
                    error_message=str(exc),
                ),
            ),
        )
        append_task_log(task_dir, "task_failed", error_type=exc.category, error=str(exc))
        notify_error = try_qq_send_text(channel_cfg, chat_id, error_message, is_group=is_group)
        if notify_error:
            append_task_log(task_dir, "task_failure_delivery_warning", errors=[f"text: {notify_error}"])
    finally:
        snapshot_runtime(task_dir)
        clear_stop_request(session_key, session_id)


def recover_interrupted_tasks() -> list[str]:
    recovered_task_ids: list[str] = []

    for processing_path in queue_processing_files():
        restored = restore_queue_file(processing_path, move=True)
        if restored is None:
            continue
        payload = read_json(restored, default={}) or {}
        recovered_task_ids.append(str(payload.get("task_id") or restored.stem))

    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue

        status_path = task_dir / "status.json"
        status = read_json(status_path, default={}) or {}
        task_payload = read_json(task_dir / "task.json", default={}) or {}
        task_id = str(task_payload.get("task_id") or status.get("task_id") or task_dir.name)
        phase = str(status.get("phase") or "").strip().lower()
        if phase in {"finished", "failed", "stopped", "requeued", "recovery-blocked"}:
            continue

        admin_lock = read_json(ADMIN_LOCK_FILE, default={}) or {}
        admin_task_id = str(admin_lock.get("task_id") or "").strip()
        if task_id and admin_task_id == task_id and is_pid_alive(admin_lock.get("pid")):
            append_task_log(task_dir, "task_recovery_skipped_admin_relay_active", admin_pid=admin_lock.get("pid"))
            continue

        inbound_dir = task_dir / "inbound"
        if not inbound_dir.is_dir():
            if phase not in {"", "starting"}:
                status["phase"] = "recovery-blocked"
                status["recovery_reason"] = "execution-may-have-started"
                status["recovery_blocked_at"] = now_iso()
                write_json(status_path, status)
                append_task_log(task_dir, "task_recovery_blocked", phase=phase)
            continue

        if phase not in {"", "starting"}:
            channel = str(task_payload.get("channel") or "qq").strip() or "qq"
            recovery_task_id = new_task_id(channel)
            original_request = str(task_payload.get("user_request") or "").strip()
            recovery_message = (
                f"[SYSTEM RECOVERY] Previous task {task_id} was interrupted during phase '{phase}'. "
                f"Inspect the existing task directory '{task_dir}' first, then continue safely without blindly repeating completed side effects.\n\n"
                f"Original request:\n{original_request}"
            ).strip()
            recovery_metadata = dict(task_payload.get("metadata") or {}) if isinstance(task_payload.get("metadata"), dict) else {}
            recovery_phase1 = dict(recovery_metadata.get("phase1") or {}) if isinstance(recovery_metadata.get("phase1"), dict) else {}
            recovery_phase1.update(
                {
                    "recovery_resume": True,
                    "interrupted_task_id": task_id,
                    "interrupted_phase": phase,
                    "interrupted_task_dir": str(task_dir),
                }
            )
            recovery_metadata["phase1"] = recovery_phase1
            recovery_task = {
                **task_payload,
                "task_id": recovery_task_id,
                "received_at": now_iso(),
                "received_ts": time.time(),
                "user_request": recovery_message,
                "message_id": str(task_payload.get("message_id") or f"{task_id}-recovery").strip(),
                "routing_mode": "flush",
                "metadata": recovery_metadata,
                "recovery_of_task_id": task_id,
            }
            recovery_path = queue_task_path(recovery_task)
            write_json(recovery_path, recovery_task)
            recovered_at = now_iso()
            status["phase"] = "requeued"
            status["recovered_at"] = recovered_at
            status["recovery_reason"] = "resume-after-interruption"
            status["recovery_replacement_task_id"] = recovery_task_id
            write_json(status_path, status)
            append_task_log(
                task_dir,
                "task_requeued_after_recovery",
                recovered_at=recovered_at,
                recovery_task_id=recovery_task_id,
                mode="resume",
                interrupted_phase=phase,
            )
            recovered_task_ids.append(recovery_task_id)
            continue

        restored_count = 0
        for inbound_path in sorted(inbound_dir.glob("*.json")):
            if (QUEUE_PENDING_DIR / inbound_path.name).exists():
                continue
            if (QUEUE_PROCESSING_DIR / inbound_path.name).exists():
                continue
            restored = restore_queue_file(inbound_path, move=False)
            if restored is None:
                continue
            restored_count += 1

        if restored_count == 0:
            continue

        recovered_at = now_iso()
        status["phase"] = "requeued"
        status["recovered_at"] = recovered_at
        status["recovery_reason"] = "worker-startup"
        write_json(status_path, status)
        append_task_log(task_dir, "task_requeued_after_recovery", restored_count=restored_count, recovered_at=recovered_at)

        session_key = str(task_payload.get("session_key") or status.get("session_key") or "").strip()
        session_id = str(task_payload.get("session_id") or status.get("session_id") or "").strip()
        project_id = str(task_payload.get("project_id") or status.get("project_id") or "").strip()
        project_root = str(task_payload.get("project_root") or status.get("project_root") or "").strip()
        chat_id = str(task_payload.get("chat_id") or "").strip()

        if session_key:
            session_state = get_session_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=str(task_payload.get("channel") or "qq"),
                default_project_id=project_id or "phase1-remote-dev",
                default_project_root=project_root or str(ROOT),
            )
            release_active_session(session_state, session_id)
            session_state["last_task_id"] = task_id
            session_state["last_progress"] = "requeued"
            session_state["last_result"] = "任务中断后已自动重新入队，等待继续处理。"
            save_session_state(session_key, session_state)
            append_session_event(
                session_key,
                session_id or str(session_state.get("current_session_id") or ""),
                {
                    "ts": recovered_at,
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": "任务中断后已自动重新入队，等待继续处理。",
                    "status": "requeued",
                },
            )

        if project_id:
            project_state = get_project_state(project_id, project_root or str(ROOT), session_key, session_id)
            project_state["last_task_id"] = task_id
            project_state["last_result"] = "任务中断后已自动重新入队，等待继续处理。"
            save_project_state(project_id, project_state)

        recovered_task_ids.append(task_id)

    return recovered_task_ids


def compose_final_message(claude_text: str, review_note: str, artifacts: dict[str, list[str]]) -> str:
    # IMPORTANT: put actual content first so first_nonempty_line() extracts the real text, not the "1. 结果" label
    parts = [
        normalize_qq_text(claude_text.strip() or "任务已完成。"),
        "1. 结果",
    ]
    section_index = 2
    if review_note:
        parts.extend(
            [
                f"{section_index}. 审查与验证",
                normalize_qq_text(review_note.strip()),
            ]
        )
        section_index += 1
    if artifacts["urls"]:
        parts.append(f"{section_index}. 相关链接")
        parts.extend(f"{idx}. {item}" for idx, item in enumerate(artifacts["urls"], 1))
        section_index += 1
    if artifacts["notes"]:
        parts.append(f"{section_index}. 补充说明")
        parts.extend(f"{idx}. {normalize_qq_text(item)}" for idx, item in enumerate(artifacts["notes"], 1))
    return "\n".join(part for part in parts if part)


def git_repo_ready(project_cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def extract_plan_change_scope(codex_plan_result: dict[str, Any]) -> str:
    payload = codex_plan_result.get("json") if isinstance(codex_plan_result, dict) else None
    if not isinstance(payload, dict):
        return "unknown"
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return "unknown"
    scope = str(plan.get("change_scope") or "unknown").strip().lower()
    if scope in {"none", "project", "unknown"}:
        return scope
    return "unknown"


def should_skip_codex_review(
    batch: list[dict[str, Any]],
    codex_plan_result: dict[str, Any],
    *,
    used_admin_path: bool,
) -> bool:
    if used_admin_path:
        return False
    if len(batch) != 1:
        return False

    seed_task = batch[0] if batch else {}
    metadata = seed_task.get("metadata") if isinstance(seed_task.get("metadata"), dict) else {}
    phase1_metadata = metadata.get("phase1") if isinstance(metadata.get("phase1"), dict) else {}
    system_action = str(seed_task.get("system_action") or "").strip()
    if system_action == "authorized_ai_file_search" or phase1_metadata.get("authorized_computer_search"):
        return True

    if gather_batch_attachments(batch):
        return False
    if extract_plan_change_scope(codex_plan_result) != "none":
        return False

    request_text = build_batch_request(batch).strip()
    if len(request_text) > 240:
        return False

    artifact_payload = read_json(ARTIFACTS_FILE, default={"files": [], "urls": [], "notes": []}) or {}
    if not isinstance(artifact_payload, dict):
        return False
    if artifact_payload.get("files") or artifact_payload.get("urls"):
        return False
    return True


def collect_batch(
    seed_task: dict[str, Any],
    processing_path: Path,
    task_dir: Path,
    debounce_seconds: int,
    max_batch_items: int,
) -> list[dict[str, Any]]:
    archive_claimed_queue_file(processing_path, task_dir)
    batch = [seed_task]
    if seed_task.get("routing_mode") != "collect" or max_batch_items <= 1:
        return batch

    deadline = time.time() + debounce_seconds
    while time.time() < deadline:
        claimed = claim_matching_pending_tasks(seed_task)
        if not claimed:
            time.sleep(1)
            continue
        for task, path in claimed:
            archive_claimed_queue_file(path, task_dir)
            batch.append(task)
            if len(batch) >= max_batch_items:
                batch.sort(key=lambda item: float(item.get("received_ts") or 0.0))
                return batch
        if any(task.get("routing_mode") == "flush" or (task.get("attachments") or []) for task, _ in claimed):
            break
        deadline = time.time() + debounce_seconds
    batch.sort(key=lambda item: float(item.get("received_ts") or 0.0))
    return batch


def run_claude_stage(
    prompt: str,
    project_cwd: Path,
    task_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
    channel_cfg: dict[str, Any],
    chat_id: str,
    is_group: bool,
    extra_system_prompt: str,
    session_key: str,
    session_id: str,
    heartbeat_seconds: int,
    admin_request_file: Path | None = None,
) -> dict[str, Any]:
    if admin_request_file is not None and admin_request_file.exists():
        admin_request_file.unlink()

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

        last_mtime = QQ_PROGRESS_FILE.stat().st_mtime if QQ_PROGRESS_FILE.exists() else 0.0
        last_heartbeat = time.time()

        while process.poll() is None:
            time.sleep(WORKER_POLL_SECONDS)
            now = time.time()

            stop_payload = read_stop_request(session_key, session_id)
            if stop_payload:
                stop_process_tree(process)
                return {
                    "returncode": process.returncode,
                    "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                    "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                    "stopped": True,
                    "stop_payload": stop_payload,
                }

            if admin_request_file is not None and admin_request_file.exists():
                admin_payload = read_json(admin_request_file, default={}) or {}
                if admin_payload.get("requires_admin"):
                    stop_process_tree(process)
                    return {
                        "returncode": process.returncode,
                        "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                        "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                        "admin_requested": True,
                        "admin_payload": admin_payload,
                    }

            current_mtime = QQ_PROGRESS_FILE.stat().st_mtime if QQ_PROGRESS_FILE.exists() else 0.0
            if current_mtime > last_mtime:
                last_mtime = current_mtime
                last_heartbeat = now
            elif now - last_heartbeat >= heartbeat_seconds:
                last_heartbeat = now
                progress = QQ_PROGRESS_FILE.read_text(encoding="utf-8").strip() if QQ_PROGRESS_FILE.exists() else "任务仍在运行。"
                try_qq_send_text(channel_cfg, chat_id, progress, is_group=is_group)

        payload = {
            "returncode": process.returncode,
            "stdout": stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
            "stderr": stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
        }
        if admin_request_file is not None and admin_request_file.exists():
            admin_payload = read_json(admin_request_file, default={}) or {}
            if admin_payload.get("requires_admin"):
                payload["admin_requested"] = True
                payload["admin_payload"] = admin_payload
        return payload


def process_task(seed_task: dict[str, Any], processing_path: Path, config: dict[str, Any], settings: dict[str, Any]) -> None:
    heartbeat_seconds = int(settings["heartbeat"].get("intervalSeconds") or 30 * 60)
    debounce_seconds = int(settings["session"].get("debounceSeconds") or 18)
    recent_context_limit = int(settings["session"].get("recentContextItems") or 8)
    max_batch_items = int(settings["session"].get("maxBatchItems") or 20)
    channel_cfg = config["channels"]["qq"]
    chat_id = str(seed_task.get("chat_id") or "").strip()
    is_group = is_group_task(seed_task)
    task_id = str(seed_task.get("task_id") or new_task_id()).strip()
    task_dir = TASKS_DIR / task_id
    ensure_dir(task_dir)

    if is_synthetic_review_artifact_task(seed_task):
        started_at = now_iso()
        discarded_task_name = truncate(seed_task.get("user_request", "synthetic review artifact"), limit=120)
        discarded_session_key = str(seed_task.get("session_key") or "")
        discarded_session_id = str(seed_task.get("session_id") or "")
        discarded_project_id = str(seed_task.get("project_id") or "")
        discarded_project_root = str(seed_task.get("project_root") or "")
        write_json(task_dir / "task.json", seed_task)
        append_task_log(
            task_dir,
            "synthetic_review_task_discarded",
            source_task_file=str(seed_task.get("source_task_file") or ""),
            session_key=discarded_session_key,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="discarded",
                task_name=discarded_task_name,
                chat_id=chat_id,
                project_id=discarded_project_id,
                project_root=discarded_project_root,
                session_key=discarded_session_key,
                session_id=discarded_session_id,
                started_at=started_at,
                finished_at=started_at,
                error="discarded synthetic review artifact task",
                error_type="synthetic-review-artifact",
                ack="discarded",
                reply_code="synthetic_review_discarded",
                user_visible_status="failed",
                message="系统已丢弃 synthetic review artifact 任务。",
            ),
        )
        if discarded_session_key and discarded_session_id:
            merge_task_outcome_state(
                session_key=discarded_session_key,
                chat_id=chat_id,
                channel=str(seed_task.get("channel") or "qq"),
                default_project_id=discarded_project_id or "phase1-remote-dev",
                default_project_root=discarded_project_root or str(ROOT),
                project_id=discarded_project_id,
                project_root=discarded_project_root,
                session_id=discarded_session_id,
                task_id=task_id,
                result_text="系统已丢弃 synthetic review artifact 任务。",
                progress="discarded",
                reply_code="synthetic_review_discarded",
                failure_category="worker_failed",
                finished_at=started_at,
            )
            append_session_event(
                discarded_session_key,
                discarded_session_id,
                payload_with_receipt(
                    {
                        "ts": started_at,
                        "type": "task_status",
                        "task_id": task_id,
                        "detail": "系统已丢弃 synthetic review artifact 任务。",
                        "error_type": "synthetic-review-artifact",
                    },
                    build_receipt(
                        stage="worker",
                        ack="discarded",
                        message="系统已丢弃 synthetic review artifact 任务。",
                        task_id=task_id,
                        session_key=discarded_session_key,
                        session_id=discarded_session_id,
                        project_id=discarded_project_id,
                        project_root=discarded_project_root,
                        phase="discarded",
                        reply_code="synthetic_review_discarded",
                        user_visible_status="failed",
                        failure_category="worker_failed",
                        error_code="synthetic-review-artifact",
                        error_message="discarded synthetic review artifact task",
                    ),
                ),
            )
        processing_path.unlink(missing_ok=True)
        return

    if is_health_probe_task(seed_task):
        handle_health_probe_task(seed_task, processing_path, config, settings)
        return

    if str(seed_task.get("system_action") or "").strip() == "send_local_file":
        handle_send_local_file_task(seed_task, processing_path, config, settings)
        return

    batch = collect_batch(seed_task, processing_path, task_dir, debounce_seconds, max_batch_items)
    task_name = truncate(seed_task.get("user_request", "未命名任务"), limit=120)
    session_key = str(seed_task.get("session_key") or "").strip()
    session_id = str(seed_task.get("session_id") or "").strip()
    project_id, project_root, project_cwd, default_project_root = resolve_task_project_context(seed_task, config, settings)
    seed_task["project_id"] = project_id
    seed_task["project_root"] = project_root
    trusted_search_roots = trusted_authorized_search_roots(
        seed_task,
        config,
        settings,
        project_root,
        default_project_root=default_project_root,
    )
    apply_trusted_search_roots(seed_task, trusted_search_roots)
    task_started_at = now_iso()

    reset_artifacts()
    write_json(task_dir / "task.json", seed_task)
    write_json(task_dir / "batch.json", batch)
    append_task_log(task_dir, "task_claimed", queue_depth=queue_depth(), batch_size=len(batch))

    session_state, project_state = bind_running_task_state(
        session_key=session_key,
        chat_id=chat_id,
        channel=str(seed_task.get("channel") or "qq"),
        default_project_id=project_id,
        default_project_root=project_root,
        project_id=project_id,
        project_root=project_root,
        session_id=session_id,
        task_id=task_id,
    )

    write_worker_lock(
        {
            "pid": os.getpid(),
            "phase": "starting",
            "active_task_id": task_id,
            "project_id": project_id,
            "session_key": session_key,
            "session_id": session_id,
            "worker_stdout_log": str(RUNTIME_DIR / "worker.out.log"),
            "worker_stderr_log": str(RUNTIME_DIR / "worker.err.log"),
            "started_at": task_started_at,
        }
    )

    update_runtime_state(
        task_name=task_name,
        status="running",
        progress=format_progress("已接单", task_name, f"当前项目：{project_id}\n当前会话：{session_id}\n本轮合并消息：{len(batch)} 条"),
        owner="Claude Code",
        project_id=project_id,
        session_id=session_id,
        heartbeat_interval_seconds=heartbeat_seconds,
        started_at=task_started_at,
    )
    save_status(
        task_dir,
        build_worker_status_payload(
            task_id=task_id,
            phase="starting",
            task_name=task_name,
            chat_id=chat_id,
            project_id=project_id,
            project_root=str(project_cwd),
            session_key=session_key,
            session_id=session_id,
            started_at=task_started_at,
            batch_size=len(batch),
            attachments=gather_batch_attachments(batch),
            ack="running",
            reply_code="running",
            user_visible_status="running",
            message="任务已接收，正在准备执行。",
        ),
    )

    try_qq_send_text(
        channel_cfg,
        chat_id,
        f"已收到任务，当前项目是“{project_id}”，会话是“{session_id}”。这轮共合并 {len(batch)} 条输入，先由 Codex 做拆解，再交给 Claude Code 主执行。",
        is_group=is_group,
    )

    recent_context_text = format_recent_context(recent_session_events(session_key, session_id, recent_context_limit))

    try:
        queued_stop_request = read_stop_request(session_key, session_id)
        if queued_stop_request:
            append_task_log(task_dir, "task_stopped_before_start", reason=queued_stop_request.get("reason", ""))
            raise Phase1Error("user-stop", f"用户在任务启动前请求停止：{queued_stop_request.get('reason', '未提供原因')}")

        codex_plan_result = run_codex_plan(
            seed_task,
            task_dir,
            batch,
            project_cwd,
            trusted_search_roots=trusted_search_roots,
        )
        if codex_plan_result.get("stopped"):
            stop_payload = codex_plan_result.get("stop_payload") or {}
            append_task_log(task_dir, "codex_plan_stopped", reason=stop_payload.get("reason", ""))
            raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
        codex_plan_json = codex_plan_result.get("json") or {}
        codex_plan_text = (
            (codex_plan_json.get("rawOutput") or "").strip()
            or codex_plan_result.get("stdout", "").strip()
            or "本轮没有可用的 Codex 规划输出。"
        )
        append_task_log(task_dir, "codex_plan_finished", ok=bool(codex_plan_result.get("ok")))

        if codex_plan_result.get("ok"):
            update_runtime_state(
                task_name,
                "running",
                format_progress("规划完成", task_name, "Codex 已完成只读拆解，Claude Code 开始主执行。"),
                "Claude Code",
                project_id,
                session_id,
                heartbeat_seconds,
                started_at=task_started_at,
            )
            try_qq_send_text(channel_cfg, chat_id, "Codex 已完成任务拆解，Claude Code 开始主执行。", is_group=is_group)
        else:
            codex_plan_text = "Codex 规划阶段失败，本轮改由 Claude 直接执行。\n\n" + codex_plan_result.get("stderr", "")
            update_runtime_state(
                task_name,
                "running",
                format_progress("直接执行", task_name, "Codex 规划没有成功，这轮改由 Claude Code 直接执行。"),
                "Claude Code",
                project_id,
                session_id,
                heartbeat_seconds,
                started_at=task_started_at,
            )
            try_qq_send_text(channel_cfg, chat_id, "Codex 规划没有成功，这轮改由 Claude Code 直接执行。", is_group=is_group)

        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="claude-main",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                batch_size=len(batch),
                ack="running",
                reply_code="claude_main",
                user_visible_status="running",
                message="任务已进入 Claude 主执行阶段。",
                extra_meta={"codex_plan_ok": bool(codex_plan_result.get("ok"))},
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "claude-main",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )

        extra_system_prompt = REMOTE_PROMPT_FILE.read_text(encoding="utf-8") if REMOTE_PROMPT_FILE.exists() else ""
        admin_request_file = task_dir / ADMIN_REQUEST_FILE_NAME
        review_note = ""
        use_admin_path, admin_source = should_use_admin_path(seed_task, codex_plan_text)
        used_admin_path = False

        if use_admin_path:
            used_admin_path = True
            update_runtime_state(
                task_name,
                "running",
                format_progress("权限切换", task_name, "Codex 判断这轮任务需要管理员权限，正在切到管理员中继通道。"),
                "Claude Code",
                project_id,
                session_id,
                heartbeat_seconds,
                started_at=task_started_at,
            )
            try_qq_send_text(channel_cfg, chat_id, "这轮任务需要管理员权限，正在自动切到管理员执行通道。", is_group=is_group)
            admin_result = run_admin_relay(
                task=seed_task,
                batch=batch,
                task_dir=task_dir,
                codex_plan_text=codex_plan_text,
                reason=f"preflight:{admin_source}",
                trigger_note="Codex 预判这轮任务需要管理员权限。",
                channel_cfg=channel_cfg,
                chat_id=chat_id,
                is_group=is_group,
                heartbeat_seconds=heartbeat_seconds,
            )
            if admin_result.get("phase") == "stopped":
                stop_payload = admin_result.get("stop_payload") or {}
                raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
            if admin_result.get("phase") == "failed":
                raise Phase1Error("admin-relay-failed", admin_result.get("stderr") or admin_result.get("result_text") or "管理员中继失败。")
            claude_stdout = (admin_result.get("stdout") or admin_result.get("result_text") or "").strip()
            claude_stderr = admin_result.get("stderr", "").strip()
        else:
            main_prompt = build_claude_prompt(
                task=seed_task,
                batch=batch,
                codex_plan=codex_plan_text,
                recent_context_text=recent_context_text,
                admin_escalation_file=admin_request_file,
                trusted_search_roots=trusted_search_roots,
            )
            claude_result = run_claude_stage(
                prompt=main_prompt,
                project_cwd=project_cwd,
                task_dir=task_dir,
                stdout_path=task_dir / "claude.stdout.log",
                stderr_path=task_dir / "claude.stderr.log",
                channel_cfg=channel_cfg,
                chat_id=chat_id,
                is_group=is_group,
                extra_system_prompt=extra_system_prompt,
                session_key=session_key,
                session_id=session_id,
                heartbeat_seconds=heartbeat_seconds,
                admin_request_file=admin_request_file,
            )

            if claude_result.get("stopped"):
                stop_payload = claude_result.get("stop_payload") or {}
                raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")

            if claude_result.get("admin_requested"):
                used_admin_path = True
                admin_payload = claude_result.get("admin_payload") or {}
                trigger_note = str(admin_payload.get("reason", "")).strip() or "Claude 在执行中确认需要管理员权限。"
                update_runtime_state(
                    task_name,
                    "running",
                    format_progress("权限切换", task_name, "Claude 执行中确认需要管理员权限，正在切到管理员中继通道。"),
                    "Claude Code",
                    project_id,
                    session_id,
                    heartbeat_seconds,
                    started_at=task_started_at,
                )
                try_qq_send_text(channel_cfg, chat_id, "Claude 执行中确认需要管理员权限，正在自动切到管理员执行通道。", is_group=is_group)
                admin_result = run_admin_relay(
                    task=seed_task,
                    batch=batch,
                    task_dir=task_dir,
                    codex_plan_text=codex_plan_text,
                    reason="runtime_claude_escalation",
                    trigger_note=trigger_note,
                    channel_cfg=channel_cfg,
                    chat_id=chat_id,
                    is_group=is_group,
                    heartbeat_seconds=heartbeat_seconds,
                )
                if admin_result.get("phase") == "stopped":
                    stop_payload = admin_result.get("stop_payload") or {}
                    raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
                if admin_result.get("phase") == "failed":
                    raise Phase1Error("admin-relay-failed", admin_result.get("stderr") or admin_result.get("result_text") or "管理员中继失败。")
                claude_stdout = (admin_result.get("stdout") or admin_result.get("result_text") or "").strip()
                claude_stderr = admin_result.get("stderr", "").strip()
            else:
                claude_stdout = claude_result.get("stdout", "").strip()
                claude_stderr = claude_result.get("stderr", "").strip()
                if claude_result.get("returncode") != 0 and not claude_stdout:
                    raise Phase1Error("claude-main-failed", f"Claude 执行失败：{claude_stderr or 'unknown error'}")

        if "claude_result" in locals():
            if not claude_result.get("admin_requested") and claude_result.get("returncode") != 0:
                raise Phase1Error(
                    "claude-main-failed",
                    f"Claude 执行失败：{claude_stderr or claude_stdout or 'unknown error'}",
                )

        if should_skip_codex_review(batch, codex_plan_result, used_admin_path=used_admin_path):
            review_note = "本轮判定为纯文本或状态类请求，已跳过 Codex 复审快路径。"
        else:  # run_codex_review() already handles both git and non-git workspaces.
            write_worker_lock(
                {
                    "pid": os.getpid(),
                    "phase": "codex-review",
                    "active_task_id": task_id,
                    "project_id": project_id,
                    "session_key": session_key,
                    "session_id": session_id,
                    "started_at": task_started_at,
                }
            )
            update_runtime_state(
                task_name,
                "running",
                format_progress("审查中", task_name, "Claude 主执行已完成，正在做 Codex 对抗式审查。"),
                "Claude Code",
                project_id,
                session_id,
                heartbeat_seconds,
                started_at=task_started_at,
            )
            try_qq_send_text(channel_cfg, chat_id, "Claude 主执行已完成，正在做 Codex 对抗式审查。", is_group=is_group)

            first_review = run_codex_review(task_dir, project_cwd, stop_context=(session_key, session_id))
            if first_review.get("stopped"):
                stop_payload = first_review.get("stop_payload") or {}
                append_task_log(task_dir, "codex_review_stopped", reason=stop_payload.get("reason", ""))
                raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
            review_payload = first_review.get("json") or {}
            append_task_log(task_dir, "codex_review_finished", ok=bool(first_review.get("ok")))
            if not first_review.get("ok"):
                if first_review.get("timed_out"):
                    review_note = "Codex 对抗式审查超时，这轮先按 Claude 结果回传。"
                else:
                    review_note = "Codex 对抗式审查失败，这轮先按 Claude 结果回传。"
            elif review_needs_attention(first_review):
                review_summary = build_review_summary(review_payload)
                update_runtime_state(
                    task_name,
                    "running",
                    format_progress("修正中", task_name, "Codex 审查发现问题，Claude Code 正在修正。"),
                    "Claude Code",
                    project_id,
                    session_id,
                    heartbeat_seconds,
                    started_at=task_started_at,
                )
                try_qq_send_text(channel_cfg, chat_id, "Codex 审查发现了需要修正的点，Claude Code 正在修正。", is_group=is_group)
                fix_prompt = build_claude_prompt(
                    task=seed_task,
                    batch=batch,
                    codex_plan=codex_plan_text,
                    recent_context_text=recent_context_text,
                    review_feedback=review_summary,
                    admin_escalation_file=admin_request_file,
                    trusted_search_roots=trusted_search_roots,
                )
                fix_result = run_claude_stage(
                    prompt=fix_prompt,
                    project_cwd=project_cwd,
                    task_dir=task_dir,
                    stdout_path=task_dir / "claude.stdout.log",
                    stderr_path=task_dir / "claude.stderr.log",
                    channel_cfg=channel_cfg,
                    chat_id=chat_id,
                    is_group=is_group,
                    extra_system_prompt=extra_system_prompt,
                    session_key=session_key,
                    session_id=session_id,
                    heartbeat_seconds=heartbeat_seconds,
                    admin_request_file=admin_request_file,
                )
                if fix_result.get("stopped"):
                    raise Phase1Error("user-stop", "用户在修正阶段请求停止当前任务。")
                if fix_result.get("admin_requested"):
                    used_admin_path = True
                    admin_payload = fix_result.get("admin_payload") or {}
                    trigger_note = str(admin_payload.get("reason", "")).strip() or "Claude 在修正阶段确认需要管理员权限。"
                    admin_result = run_admin_relay(
                        task=seed_task,
                        batch=batch,
                        task_dir=task_dir,
                        codex_plan_text=codex_plan_text,
                        reason="runtime_fix_escalation",
                        trigger_note=trigger_note,
                        channel_cfg=channel_cfg,
                        chat_id=chat_id,
                        is_group=is_group,
                        heartbeat_seconds=heartbeat_seconds,
                    )
                    if admin_result.get("phase") == "stopped":
                        stop_payload = admin_result.get("stop_payload") or {}
                        raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
                    if admin_result.get("phase") == "failed":
                        raise Phase1Error("admin-relay-failed", admin_result.get("stderr") or admin_result.get("result_text") or "管理员中继失败。")
                    claude_stdout = (admin_result.get("stdout") or admin_result.get("result_text") or "").strip()
                    claude_stderr = admin_result.get("stderr", "").strip()
                else:
                    claude_stdout = (fix_result.get("stdout") or claude_stdout).strip()
                    claude_stderr = (fix_result.get("stderr") or claude_stderr).strip()

                if "fix_result" in locals():
                    if not fix_result.get("admin_requested") and fix_result.get("returncode") != 0:
                        raise Phase1Error(
                            "claude-fix-failed",
                            f"Claude 修正阶段失败：{claude_stderr or claude_stdout or 'unknown error'}",
                        )

                second_review = run_codex_review(task_dir, project_cwd, stop_context=(session_key, session_id))
                if second_review.get("stopped"):
                    stop_payload = second_review.get("stop_payload") or {}
                    append_task_log(task_dir, "codex_review_stopped", reason=stop_payload.get("reason", ""))
                    raise Phase1Error("user-stop", f"用户请求停止当前任务：{stop_payload.get('reason', '未提供原因')}")
                second_payload = second_review.get("json") or {}
                if not second_review.get("ok"):
                    if second_review.get("timed_out"):
                        review_note = "Codex 复审超时，建议交付前人工再看一轮。"
                    else:
                        review_note = "Codex 复审失败，建议交付前人工再看一轮。"
                elif review_needs_attention(second_review):
                    review_note = "Codex 复审后仍有剩余风险，建议交付前再做一次人工确认。"
                else:
                    summary = (second_payload.get("result") or {}).get("summary", "")
                    review_note = summary or "Codex 复审通过，没有新增阻断级问题。"
            else:
                summary = (review_payload.get("result") or {}).get("summary", "")
                review_note = summary or "Codex 审查通过，没有新增阻断级问题。"

        artifacts = package_artifacts_for_qq(
            task_dir,
            task_id,
            settings,
            project_root=project_root,
            extra_allowed_roots=artifact_extra_roots_for_task(seed_task, trusted_search_roots=trusted_search_roots),
        )
        final_message = compose_final_message(claude_stdout or "任务已完成。", review_note, artifacts)

        update_runtime_state(
            task_name,
            "finished",
            format_progress("完成", task_name, "任务已完成，结果正在回传到 QQ。"),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        finished_at = now_iso()
        summary_text = first_nonempty_line(final_message, "任务已完成。")
        status_payload = build_worker_status_payload(
            task_id=task_id,
            phase="finished",
            task_name=task_name,
            project_id=project_id,
            project_root=str(project_cwd),
            session_key=session_key,
            session_id=session_id,
            started_at=task_started_at,
            finished_at=finished_at,
            result=summary_text,
            artifacts=artifacts,
            ack="finished",
            reply_code="completed",
            user_visible_status="completed",
            message=summary_text,
        )
        save_status(
            task_dir,
            status_payload,
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "finished",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        session_state, project_state = merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=summary_text,
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
                    "ts": now_iso(),
                    "type": "assistant_result",
                    "task_id": task_id,
                    "summary": summary_text,
                    "artifacts": artifacts,
                },
                build_receipt(
                    stage="worker",
                    ack="finished",
                    message=summary_text,
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
        append_task_log(task_dir, "task_finished", review_note=review_note, artifact_files=artifacts["files"])
        delivery_errors: list[str] = []
        text_error = try_qq_send_text(channel_cfg, chat_id, final_message, is_group=is_group)
        if text_error:
            delivery_errors.append(f"text: {text_error}")
        if artifacts["files"]:
            inter_file_delay_ms = int(settings["artifacts"].get("interFileDelayMs") or 0)
            file_error = try_qq_send_files(
                channel_cfg,
                chat_id,
                artifacts["files"],
                is_group=is_group,
                inter_file_delay_ms=inter_file_delay_ms,
            )
            if file_error:
                delivery_errors.append(f"files: {file_error}")
        if delivery_errors:
            warning_error_type, warning_failure_category = classify_delivery_warning(delivery_errors)
            warning_message = f"{summary_text}\n\nQQ 回执告警：{'; '.join(delivery_errors)}"
            status_payload = build_worker_status_payload(
                task_id=task_id,
                phase="finished",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                result=warning_message,
                artifacts=artifacts,
                delivery_errors=delivery_errors,
                ack="finished",
                reply_code="completed_with_delivery_warning",
                user_visible_status="completed",
                message=warning_message,
                error="; ".join(delivery_errors),
                error_type=warning_error_type,
                failure_category=warning_failure_category,
            )
            save_status(task_dir, status_payload)
            merge_task_outcome_state(
                session_key=session_key,
                chat_id=chat_id,
                channel=str(seed_task.get("channel") or "qq"),
                default_project_id=project_id,
                default_project_root=project_root,
                project_id=project_id,
                project_root=project_root,
                session_id=session_id,
                task_id=task_id,
                result_text=warning_message,
                progress="finished",
                reply_code="completed_with_delivery_warning",
                failure_category=warning_failure_category,
                finished_at=finished_at,
            )
            append_session_event(
                session_key,
                session_id,
                payload_with_receipt(
                    {
                        "ts": now_iso(),
                        "type": "task_status",
                        "task_id": task_id,
                        "detail": warning_message,
                        "delivery_errors": delivery_errors,
                    },
                    build_receipt(
                        stage="worker",
                        ack="finished",
                        message=warning_message,
                        task_id=task_id,
                        session_key=session_key,
                        session_id=session_id,
                        project_id=project_id,
                        project_root=str(project_cwd),
                        phase="finished",
                        reply_code="completed_with_delivery_warning",
                        user_visible_status="completed",
                        failure_category=warning_failure_category,
                        error_code=warning_error_type,
                        error_message="; ".join(delivery_errors),
                        meta={"delivery_errors": delivery_errors},
                    ),
                ),
            )
            append_task_log(task_dir, "task_delivery_warning", errors=delivery_errors)
        snapshot_runtime(task_dir)
        clear_stop_request(session_key, session_id)
    except Phase1Error as exc:
        error_message = build_user_failure_message(exc)
        failure_category = failure_category_from_code(exc.category)
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "failed",
            format_progress("失败", task_name, f"{exc.category}: {exc}"),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type=exc.category,
                ack="stopped" if failure_category == "stopped" else "failed",
                reply_code=exc.category,
                user_visible_status="stopped" if failure_category == "stopped" else "failed",
                message=error_message,
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "failed",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        session_state, project_state = merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=error_message,
            progress="failed",
            reply_code=exc.category,
            failure_category=failure_category,
            finished_at=finished_at,
        )
        append_session_event(
            session_key,
            session_id,
            payload_with_receipt(
                {
                    "ts": now_iso(),
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": error_message,
                    "error_type": exc.category,
                },
                build_receipt(
                    stage="worker",
                    ack="stopped" if failure_category == "stopped" else "failed",
                    message=error_message,
                    task_id=task_id,
                    session_key=session_key,
                    session_id=session_id,
                    project_id=project_id,
                    project_root=str(project_cwd),
                    phase="failed",
                    reply_code=exc.category,
                    user_visible_status="stopped" if failure_category == "stopped" else "failed",
                    failure_category=failure_category,
                    error_code=exc.category,
                    error_message=str(exc),
                ),
            ),
        )
        append_task_log(task_dir, "task_failed", error_type=exc.category, error=str(exc))
        notify_error = try_qq_send_text(channel_cfg, chat_id, error_message, is_group=is_group)
        if notify_error:
            append_task_log(task_dir, "task_failure_delivery_warning", errors=[f"text: {notify_error}"])
        snapshot_runtime(task_dir)
        clear_stop_request(session_key, session_id)
    except Exception as exc:  # pragma: no cover
        error_message = f"任务执行失败：{exc}"
        finished_at = now_iso()
        update_runtime_state(
            task_name,
            "failed",
            format_progress("失败", task_name, error_message),
            "Claude Code",
            project_id,
            session_id,
            heartbeat_seconds,
            started_at=task_started_at,
        )
        save_status(
            task_dir,
            build_worker_status_payload(
                task_id=task_id,
                phase="failed",
                task_name=task_name,
                project_id=project_id,
                project_root=str(project_cwd),
                session_key=session_key,
                session_id=session_id,
                started_at=task_started_at,
                finished_at=finished_at,
                error=str(exc),
                error_type="worker-runtime",
                ack="failed",
                reply_code="worker-runtime",
                user_visible_status="failed",
                message=error_message,
            ),
        )
        write_worker_lock(
            {
                "pid": os.getpid(),
                "phase": "failed",
                "active_task_id": task_id,
                "project_id": project_id,
                "session_key": session_key,
                "session_id": session_id,
                "started_at": task_started_at,
            }
        )
        session_state, project_state = merge_task_outcome_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=str(seed_task.get("channel") or "qq"),
            default_project_id=project_id,
            default_project_root=project_root,
            project_id=project_id,
            project_root=project_root,
            session_id=session_id,
            task_id=task_id,
            result_text=error_message,
            progress="failed",
            reply_code="worker-runtime",
            failure_category="worker_failed",
            finished_at=finished_at,
        )
        append_session_event(
            session_key,
            session_id,
            payload_with_receipt(
                {
                    "ts": now_iso(),
                    "type": "task_status",
                    "task_id": task_id,
                    "detail": error_message,
                    "error_type": "worker-runtime",
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
                    reply_code="worker-runtime",
                    user_visible_status="failed",
                    failure_category="worker_failed",
                    error_code="worker-runtime",
                    error_message=str(exc),
                ),
            ),
        )
        notify_error = try_qq_send_text(channel_cfg, chat_id, error_message, is_group=is_group)
        if notify_error:
            append_task_log(task_dir, "task_failure_delivery_warning", errors=[f"text: {notify_error}"])
        append_task_log(task_dir, "task_failed", error_type="worker-runtime", error=str(exc))
        snapshot_runtime(task_dir)
        clear_stop_request(session_key, session_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config" / "nanobot.local.json"))
    args = parser.parse_args()

    config_path = Path(args.config)
    config = read_json(config_path, default=None, expand_env=True)
    if config is None:
        raise SystemExit(f"Invalid config file: {config_path}")

    ensure_runtime_layout()
    ensure_dir(TMP_DIR)
    os.environ["TEMP"] = str(TMP_DIR)
    os.environ["TMP"] = str(TMP_DIR)
    os.environ["TMPDIR"] = str(TMP_DIR)
    sanitize_codex_config()
    settings = load_phase1_settings(config)
    recovered_task_ids = recover_interrupted_tasks()
    if recovered_task_ids:
        print(f"Recovered {len(recovered_task_ids)} interrupted task(s).", flush=True)

    worker_started_at = now_iso()
    idle_announced = False
    try:
        while True:
            claimed = claim_next_pending_task()
            if claimed is None:
                if not idle_announced:
                    write_worker_lock(
                        {
                            "pid": os.getpid(),
                            "phase": "idle",
                            "active_task_id": "",
                            "project_id": "",
                            "session_key": "",
                            "session_id": "",
                            "worker_stdout_log": str(RUNTIME_DIR / "worker.out.log"),
                            "worker_stderr_log": str(RUNTIME_DIR / "worker.err.log"),
                            "started_at": worker_started_at,
                        }
                    )
                    idle_announced = True
                time.sleep(1)
                continue
            idle_announced = False
            task, processing_path = claimed
            process_task(task, processing_path, config, settings)
    finally:
        cleanup_worker_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())

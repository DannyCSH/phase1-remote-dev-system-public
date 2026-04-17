from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
QUEUE_DIR = RUNTIME_DIR / "queue"
QUEUE_PENDING_DIR = QUEUE_DIR / "pending"
QUEUE_PROCESSING_DIR = QUEUE_DIR / "processing"
PROJECTS_DIR = RUNTIME_DIR / "projects"
MANAGED_WORKSPACES_DIR = ROOT / "workspace"
SESSION_RUNTIME_DIR = RUNTIME_DIR / "sessions"
LOGS_DIR = RUNTIME_DIR / "logs"
ROUTER_LOG_DIR = LOGS_DIR / "router"
TASK_LOG_DIR = LOGS_DIR / "tasks"
CONTROL_DIR = RUNTIME_DIR / "control"
STOP_REQUEST_DIR = CONTROL_DIR / "stop"
LOCKS_DIR = CONTROL_DIR / "locks"
MESSAGE_ID_DIR = CONTROL_DIR / "message-ids"
TMP_DIR = RUNTIME_DIR / "tmp"
ADMIN_INBOX_DIR = RUNTIME_DIR / "admin-inbox"
ADMIN_TASKS_DIR = RUNTIME_DIR / "admin-tasks"
ACTIVE_TASK_FILE = RUNTIME_DIR / "ACTIVE_TASK.md"
QQ_PROGRESS_FILE = RUNTIME_DIR / "QQ_PROGRESS.md"
ARTIFACTS_FILE = RUNTIME_DIR / "LAST_ARTIFACTS.json"
WORKER_LOCK_FILE = RUNTIME_DIR / "current-worker.json"
ADMIN_LOCK_FILE = RUNTIME_DIR / "current-admin-relay.json"

IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}

PROJECT_SWITCH_RE = re.compile(r"^\s*(?:切到项目|切换项目|switch\s+project)\s*[：:]\s*(.+?)\s*$", re.I)
NEW_TASK_RE = re.compile(r"^\s*(?:新任务|new\s+task)\s*[：:]\s*(.+?)\s*$", re.I)
RESET_SESSION_RE = re.compile(r"^\s*(?:重置当前会话|重置会话|reset\s+session)\s*$", re.I)
CONTINUE_RE = re.compile(r"^\s*(?:继续当前任务|继续当前会话|continue\s+current)\s*$", re.I)
SUMMARY_RE = re.compile(r"^\s*(?:总结当前状态|当前状态|status)\s*$", re.I)
STOP_RE = re.compile(r"^\s*(?:停止当前任务|停止任务|stop\s+current)\s*$", re.I)
PROJECT_HISTORY_RE = re.compile(r"^\s*(?:历史项目|项目历史|history\s+projects?)(?:\s*[：:]\s*(.+?)\s*)?$", re.I)
LIST_PATH_RE = re.compile(r"^\s*(?:查看目录|查看文件夹|浏览目录|列出文件|browse\s+path|list\s+path)\s*[：:]\s*(.+?)\s*$", re.I)
READ_FILE_RE = re.compile(r"^\s*(?:查看文件|读取文件|read\s+file)\s*[：:]\s*(.+?)\s*$", re.I)
SEND_FILE_RE = re.compile(r"^\s*(?:发送文件|发文件|send\s+file)\s*[：:]\s*(.+?)\s*$", re.I)
WIN_PATH_RE = re.compile(r"(?P<path>[A-Za-z]:\\[^\r\n\"<>|?*]+)")
ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

ATTACHMENT_PATH_FIELDS = (
    "path",
    "local_path",
    "localPath",
    "saved_path",
    "savedPath",
    "file",
    "file_path",
    "filePath",
)
DEFAULT_LOCK_TIMEOUT_SECONDS = 15.0
DEFAULT_MESSAGE_ID_TTL_SECONDS = 7 * 24 * 60 * 60
MESSAGE_ID_PRUNE_INTERVAL_SECONDS = 15 * 60
MESSAGE_ID_PRUNE_BATCH_LIMIT = 512
WINDOWS_PERSISTENT_ENV_CACHE: dict[str, str | None] = {}
CRITICAL_JSON_FILENAMES = {
    "state.json",
    "status.json",
    "current-worker.json",
    "current-admin-relay.json",
    "LAST_ARTIFACTS.json",
    "health.json",
}

DEFAULT_PHASE1_SETTINGS: dict[str, Any] = {
    "project": {
        "defaultId": "phase1-remote-dev",
        "defaultRoot": str(ROOT),
        "allowedRoots": [
            str(ROOT),
            str(MANAGED_WORKSPACES_DIR),
        ],
    },
    "session": {
        "debounceSeconds": 18,
        "recentContextItems": 8,
        "recentMessageIdLimit": 64,
        "globalMessageIdTtlSeconds": DEFAULT_MESSAGE_ID_TTL_SECONDS,
        "maxBatchItems": 20,
    },
    "attachments": {
        "allowTextPathPromotion": False,
        "allowedRoots": [],
    },
    "artifacts": {
        "maxFiles": 0,
        "maxTotalBytes": 80 * 1024 * 1024,
        "maxSingleFileBytes": 25 * 1024 * 1024,
        "qqMaxUploadBytes": 25 * 1024 * 1024,
        "zipThreshold": 0,
        "zipNamePrefix": "phase1-artifacts",
        "allowedRoots": [],
        "preferDirectSend": True,
        "interFileDelayMs": 300,
        "directSendNoticeThreshold": 8,
    },
    "computerSearch": {
        "enabled": True,
        "allowedRoots": [],
        "historyMatchLimit": 8,
    },
    "heartbeat": {
        "intervalSeconds": 30 * 60,
    },
    "autostart": {
        "enabled": True,
        "taskName": "Phase1AutoStart",
    },
}

RECEIPT_PROTOCOL = "p1-receipt.v1"
RETRYABLE_FAILURE_CATEGORIES = {
    "gateway_unavailable",
    "router_failed",
    "worker_failed",
    "admin_relay_failed",
    "artifact_send_failed",
    "qq_api_error",
    "environment_invalid",
    "timeout",
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def failure_category_from_code(code: str) -> str:
    raw = str(code or "").strip().lower()
    if not raw:
        return ""
    if raw in {"duplicate"}:
        return "duplicate"
    if raw in {"qq_api_error", "qq-api-error", "qq-delivery-warning"}:
        return "qq_api_error"
    if raw in {"unauthorized", "unauthorized_sender", "admin-relay-unauthorized"}:
        return "unauthorized_sender"
    if raw in {"local-file-send-outside-scope", "path_outside_allowed_roots"}:
        return "unauthorized_sender"
    if raw in {"environment_invalid", "claude-missing", "codex-missing", "admin-relay-missing"}:
        return "environment_invalid"
    if raw.startswith("qq-api") or raw.startswith("qq-delivery") or "qq api" in raw or "850012" in raw:
        return "qq_api_error"
    if raw.startswith("admin-relay"):
        return "admin_relay_failed"
    if raw.startswith("router"):
        return "router_failed"
    if raw.startswith("gateway"):
        return "gateway_unavailable"
    if "timeout" in raw or raw.endswith("-timeout"):
        return "timeout"
    if raw.startswith("local-file-delivery") or raw.startswith("artifact-") or raw.startswith("local-file-send"):
        return "artifact_send_failed"
    if raw.startswith("worker"):
        return "worker_failed"
    if raw.startswith("claude") or raw.startswith("codex") or raw.startswith("review"):
        return "worker_failed"
    if raw.startswith("user-stop"):
        return "stopped"
    return "worker_failed"


def is_retryable_failure_category(category: str) -> bool:
    return str(category or "").strip() in RETRYABLE_FAILURE_CATEGORIES


def build_receipt(
    *,
    stage: str,
    ack: str,
    message: str,
    task_id: str = "",
    session_key: str = "",
    session_id: str = "",
    project_id: str = "",
    project_root: str = "",
    phase: str = "",
    reply_code: str = "",
    user_visible_status: str = "",
    failure_category: str = "",
    error_code: str = "",
    error_message: str = "",
    ts: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_failure_category = failure_category or failure_category_from_code(error_code)
    receipt: dict[str, Any] = {
        "protocol": RECEIPT_PROTOCOL,
        "stage": stage,
        "ack_stage": stage,
        "ack": ack,
        "reply_code": reply_code,
        "user_visible_status": user_visible_status,
        "failure_category": resolved_failure_category,
        "task_id": task_id,
        "session_key": session_key,
        "session_id": session_id,
        "project_id": project_id,
        "project_root": normalize_project_root(project_root),
        "message": message,
        "phase": phase,
        "ts": ts or now_iso(),
    }
    if error_code or error_message or resolved_failure_category:
        receipt["error"] = {
            "code": error_code,
            "message": error_message,
            "failure_category": resolved_failure_category,
            "retryable": is_retryable_failure_category(resolved_failure_category),
        }
    if meta:
        receipt["meta"] = meta
    return receipt


def payload_with_receipt(payload: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged["protocol"] = receipt.get("protocol", RECEIPT_PROTOCOL)
    merged["stage"] = receipt.get("stage", "")
    merged["ack_stage"] = receipt.get("ack_stage", receipt.get("stage", ""))
    merged["ack"] = receipt.get("ack", "")
    merged["reply_code"] = receipt.get("reply_code", "")
    merged["user_visible_status"] = receipt.get("user_visible_status", "")
    merged["failure_category"] = receipt.get("failure_category", "")
    merged["message"] = receipt.get("message", "")
    merged["receipt"] = receipt
    return merged


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_runtime_layout() -> None:
    for path in [
        RUNTIME_DIR,
        TASKS_DIR,
        QUEUE_DIR,
        QUEUE_PENDING_DIR,
        QUEUE_PROCESSING_DIR,
        PROJECTS_DIR,
        MANAGED_WORKSPACES_DIR,
        SESSION_RUNTIME_DIR,
        LOGS_DIR,
        ROUTER_LOG_DIR,
        TASK_LOG_DIR,
        CONTROL_DIR,
        STOP_REQUEST_DIR,
        LOCKS_DIR,
        MESSAGE_ID_DIR,
        TMP_DIR,
        ADMIN_INBOX_DIR,
        ADMIN_TASKS_DIR,
    ]:
        ensure_dir(path)

def stable_text_key(value: str | Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def hash_key(value: str | Path, length: int = 16) -> str:
    return hashlib.sha256(stable_text_key(value).encode("utf-8")).hexdigest()[:length]


def _read_lock_metadata(path: Path) -> tuple[int | None, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}

    pid_raw = payload.get("pid")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        pid = None

    created_ts = payload.get("created_ts")
    try:
        created = float(created_ts)
    except (TypeError, ValueError):
        created = path.stat().st_mtime
    return pid, created


def _lock_is_stale(path: Path, stale_after_seconds: float) -> bool:
    if not path.exists():
        return False

    pid, created_ts = _read_lock_metadata(path)
    if pid is not None:
        if is_pid_alive(pid):
            return False
        return True
    return (time.time() - created_ts) >= stale_after_seconds


@contextlib.contextmanager
def interprocess_lock(
    lock_name: str,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = 0.05,
) -> Iterator[Path]:
    ensure_runtime_layout()
    safe_name = safe_bucket_name(lock_name, "lock")
    lock_path = LOCKS_DIR / f"{safe_name}.lock"
    deadline = time.time() + max(timeout_seconds, poll_seconds)
    stale_after = max(timeout_seconds * 4, 60.0)

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock_path, stale_after):
                lock_path.unlink(missing_ok=True)
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_name}")
            time.sleep(poll_seconds)
            continue

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "created_at": now_iso(),
                            "created_ts": time.time(),
                            "lock_name": lock_name,
                        },
                        ensure_ascii=False,
                    )
                )
            break
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            lock_path.unlink(missing_ok=True)
            raise

    try:
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    temp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        temp_path.write_text(content, encoding=encoding)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def json_backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def should_write_json_backup(path: Path) -> bool:
    if path.name in CRITICAL_JSON_FILENAMES:
        return True
    parent_key = stable_text_key(path.parent)
    return parent_key in {
        stable_text_key(SESSION_RUNTIME_DIR),
        stable_text_key(PROJECTS_DIR),
        stable_text_key(RUNTIME_DIR),
        stable_text_key(CONTROL_DIR),
    }


class JsonReadError(RuntimeError):
    def __init__(self, path: Path, message: str):
        super().__init__(message)
        self.path = path


def expand_env_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = resolve_env_var(name)
            return resolved if resolved is not None else match.group(0)

        return ENV_PLACEHOLDER_RE.sub(replace, value)
    if isinstance(value, list):
        return [expand_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_placeholders(item) for key, item in value.items()}
    return value


def read_windows_persistent_env(name: str) -> str | None:
    if os.name != "nt":
        return None

    cached = WINDOWS_PERSISTENT_ENV_CACHE.get(name)
    if name in WINDOWS_PERSISTENT_ENV_CACHE:
        return cached

    try:
        import winreg
    except ImportError:
        WINDOWS_PERSISTENT_ENV_CACHE[name] = None
        return None

    value: str | None = None
    registry_locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for hive, subkey in registry_locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                raw_value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if raw_value is None:
            continue
        value = str(raw_value)
        break

    WINDOWS_PERSISTENT_ENV_CACHE[name] = value
    return value


def resolve_env_var(name: str) -> str | None:
    resolved = os.environ.get(name)
    if resolved is not None:
        return resolved
    return read_windows_persistent_env(name)


def _load_json_text(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_json(
    path: Path,
    default: Any = None,
    *,
    expand_env: bool = False,
    strict: bool = False,
    allow_backup: bool = True,
) -> Any:
    backup_path = json_backup_path(path)
    if not path.exists() and (not allow_backup or not backup_path.exists()):
        return default

    parse_error: Exception | None = None
    candidate_paths = (path, backup_path) if allow_backup else (path,)
    for candidate_path in candidate_paths:
        if candidate_path != path and not candidate_path.exists():
            continue
        try:
            payload = _load_json_text(candidate_path)
        except Exception as exc:
            if candidate_path == path:
                parse_error = exc
            continue
        return expand_env_placeholders(payload) if expand_env else payload

    if strict and parse_error is not None:
        raise JsonReadError(path, f"Invalid JSON file: {path}") from parse_error
    return default


def write_json(path: Path, payload: Any) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write_text(path, content, encoding="utf-8")
    if should_write_json_backup(path):
        atomic_write_text(json_backup_path(path), content, encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    lock_name = f"append-jsonl-{hash_key(path)}"
    with interprocess_lock(lock_name, timeout_seconds=10):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_text(path: Path, content: str) -> None:
    atomic_write_text(path, content, encoding="utf-8")


def is_pid_alive(pid_value: Any) -> bool:
    try:
        pid_int = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid_int}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except Exception:
            return False
        if result.returncode != 0:
            return False
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid_int):
                return True
        return False

    try:
        os.kill(pid_int, 0)
    except OSError:
        return False
    return True


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_phase1_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    raw_settings = {}
    if isinstance(config, dict):
        raw_settings = config.get("phase1") or {}
    merged = deep_merge(DEFAULT_PHASE1_SETTINGS, raw_settings)
    project_defaults = merged.setdefault("project", {})
    project_defaults.setdefault("defaultId", "phase1-remote-dev")
    project_defaults.setdefault("defaultRoot", str(ROOT))
    merged.setdefault("session", {}).setdefault("debounceSeconds", 18)
    merged.setdefault("session", {}).setdefault("recentContextItems", 8)
    merged.setdefault("session", {}).setdefault("recentMessageIdLimit", 64)
    merged.setdefault("session", {}).setdefault("globalMessageIdTtlSeconds", DEFAULT_MESSAGE_ID_TTL_SECONDS)
    merged.setdefault("session", {}).setdefault("maxBatchItems", 20)
    merged.setdefault("attachments", {}).setdefault("allowTextPathPromotion", False)
    merged.setdefault("attachments", {}).setdefault("allowedRoots", [])
    merged.setdefault("artifacts", {}).setdefault("maxFiles", 0)
    merged.setdefault("artifacts", {}).setdefault("maxTotalBytes", 80 * 1024 * 1024)
    merged.setdefault("artifacts", {}).setdefault("maxSingleFileBytes", 25 * 1024 * 1024)
    merged.setdefault("artifacts", {}).setdefault("qqMaxUploadBytes", 25 * 1024 * 1024)
    merged.setdefault("artifacts", {}).setdefault("zipThreshold", 0)
    merged.setdefault("artifacts", {}).setdefault("zipNamePrefix", "phase1-artifacts")
    merged.setdefault("artifacts", {}).setdefault("allowedRoots", [])
    merged.setdefault("artifacts", {}).setdefault("preferDirectSend", True)
    merged.setdefault("artifacts", {}).setdefault("interFileDelayMs", 300)
    merged.setdefault("artifacts", {}).setdefault("directSendNoticeThreshold", 8)
    merged.setdefault("computerSearch", {}).setdefault("enabled", True)
    merged.setdefault("computerSearch", {}).setdefault("allowedRoots", [])
    merged.setdefault("computerSearch", {}).setdefault("historyMatchLimit", 8)
    merged.setdefault("heartbeat", {}).setdefault("intervalSeconds", 30 * 60)
    merged.setdefault("autostart", {}).setdefault("enabled", True)
    merged.setdefault("autostart", {}).setdefault("taskName", "Phase1AutoStart")
    return merged


def safe_slug(text: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^\w\-\.]+", "-", (text or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("-._")
    return cleaned or fallback


def safe_bucket_name(text: str, fallback: str = "bucket") -> str:
    cleaned = re.sub(r"[^\w\-\.]+", "_", (text or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._")
    return cleaned or fallback


def new_session_id() -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"session-{timestamp}-{uuid.uuid4().hex[:6]}"


def new_task_id(channel: str = "qq") -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{channel}-{timestamp}-{uuid.uuid4().hex[:6]}"


def session_root(session_key: str) -> Path:
    return SESSION_RUNTIME_DIR / safe_bucket_name(session_key, "session")


def session_state_path(session_key: str) -> Path:
    return session_root(session_key) / "state.json"


def session_log_path(session_key: str, session_id: str) -> Path:
    return session_root(session_key) / f"{safe_bucket_name(session_id, 'session')}.jsonl"


def session_state_lock_name(session_key: str) -> str:
    return f"session-state-{hash_key(session_key)}"


def project_state_lock_name(project_id: str, project_root: str = "") -> str:
    seed = f"{project_id}|{normalize_project_root(project_root)}"
    return f"project-state-{hash_key(seed)}"


def project_root_hash(project_root: str | Path) -> str:
    normalized_root = normalize_project_root(project_root)
    if not normalized_root:
        return ""
    return hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()[:12]


def legacy_project_root_dir(project_id: str) -> Path:
    return PROJECTS_DIR / safe_bucket_name(project_id, "project")


def project_root_dir(project_id: str, project_root: str | Path = "") -> Path:
    bucket = safe_bucket_name(project_id, "project")
    root_hash = project_root_hash(project_root)
    if root_hash:
        return PROJECTS_DIR / f"{bucket}--{root_hash}"
    return legacy_project_root_dir(project_id)


def project_state_path(project_id: str, project_root: str | Path = "") -> Path:
    return project_root_dir(project_id, project_root) / "state.json"


def normalize_project_root(project_root: str | Path) -> str:
    raw = str(project_root or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return str(path.resolve(strict=False))


def managed_project_root(project_id: str) -> str:
    path = ensure_dir(MANAGED_WORKSPACES_DIR / safe_slug(project_id, "project"))
    return str(path.resolve(strict=False))


def iter_project_state_paths(project_id: str, project_root: str | Path = "") -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []

    explicit_root = normalize_project_root(project_root)
    if explicit_root:
        explicit_path = project_state_path(project_id, explicit_root)
        candidates.append(explicit_path)
        seen.add(stable_text_key(explicit_path))

    legacy_path = project_state_path(project_id)
    if stable_text_key(legacy_path) not in seen:
        candidates.append(legacy_path)
        seen.add(stable_text_key(legacy_path))

    prefix = safe_bucket_name(project_id, "project")
    for candidate_dir in sorted(PROJECTS_DIR.glob(f"{prefix}*"), key=lambda item: item.stat().st_mtime, reverse=True):
        candidate_path = candidate_dir / "state.json"
        candidate_key = stable_text_key(candidate_path)
        if candidate_key in seen:
            continue
        candidates.append(candidate_path)
        seen.add(candidate_key)
    return candidates


def read_project_state(project_id: str, project_root: str | Path = "") -> dict[str, Any] | None:
    for candidate_path in iter_project_state_paths(project_id, project_root):
        payload = read_json(candidate_path, default=None)
        if isinstance(payload, dict):
            return payload
    return None


def is_path_within(candidate: str | Path, root: str | Path) -> bool:
    candidate_normalized = normalize_project_root(candidate)
    root_normalized = normalize_project_root(root)
    if not candidate_normalized or not root_normalized:
        return False
    candidate_path = Path(candidate_normalized)
    root_path = Path(root_normalized)
    try:
        candidate_path.relative_to(root_path)
        return True
    except ValueError:
        return False


def collect_allowed_project_roots(settings: dict[str, Any] | None, default_project_root: str) -> list[str]:
    roots = [default_project_root, str(MANAGED_WORKSPACES_DIR)]
    if isinstance(settings, dict):
        project_cfg = settings.get("project") if isinstance(settings.get("project"), dict) else {}
        extra_roots = project_cfg.get("allowedRoots", []) if isinstance(project_cfg, dict) else []
        if isinstance(extra_roots, list):
            roots.extend(str(item) for item in extra_roots if str(item).strip())

    normalized_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = normalize_project_root(root)
        if not normalized:
            continue
        key = stable_text_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        normalized_roots.append(normalized)
    return normalized_roots


def config_restricts_to_workspace(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return True
    tools_cfg = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    return bool(tools_cfg.get("restrictToWorkspace", True))


def build_local_file_access_roots(
    config: dict[str, Any] | None,
    settings: dict[str, Any],
    project_root: str,
    default_project_root: str = "",
) -> list[str]:
    normalized_project_root = normalize_project_root(project_root)
    normalized_default_root = normalize_project_root(default_project_root) or normalized_project_root or str(ROOT)
    workspace_roots = collect_allowed_project_roots(settings, normalized_project_root or normalized_default_root)
    computer_search_cfg = settings.get("computerSearch") if isinstance(settings.get("computerSearch"), dict) else {}
    configured_roots = computer_search_cfg.get("allowedRoots", []) if isinstance(computer_search_cfg, dict) else []
    explicit_roots = [str(item) for item in configured_roots if str(item).strip()] if isinstance(configured_roots, list) else []
    if explicit_roots:
        return list_local_drive_roots(workspace_roots + explicit_roots)
    if config_restricts_to_workspace(config):
        return workspace_roots
    return list_local_drive_roots()


def is_allowed_project_root(project_root: str, allowed_roots: list[str]) -> bool:
    normalized = normalize_project_root(project_root)
    if not normalized:
        return False
    return any(is_path_within(normalized, root) for root in allowed_roots)


def resolve_project_root(
    project_id: str,
    requested_root: str,
    default_project_id: str,
    default_project_root: str,
    settings: dict[str, Any] | None = None,
) -> str:
    normalized_default = normalize_project_root(default_project_root)
    allowed_roots = collect_allowed_project_roots(settings, normalized_default)
    normalized_requested = normalize_project_root(requested_root)
    if normalized_requested:
        if not is_allowed_project_root(normalized_requested, allowed_roots):
            raise ValueError(f"Requested project root is outside allowed roots: {normalized_requested}")
        return normalized_requested

    existing_state = read_project_state(project_id)
    if isinstance(existing_state, dict):
        existing_root = normalize_project_root(str(existing_state.get("project_root") or ""))
        if existing_root and is_allowed_project_root(existing_root, allowed_roots):
            if project_id != default_project_id and normalized_default and existing_root == normalized_default:
                return managed_project_root(project_id)
            return existing_root

    if project_id == default_project_id:
        if normalized_default:
            return normalized_default

    return managed_project_root(project_id)


def project_execution_dir(project_root: str) -> Path:
    normalized_root = normalize_project_root(project_root)
    if not normalized_root:
        return ROOT

    path = Path(normalized_root)
    if path.exists() and path.is_file():
        return path.parent

    if not path.exists():
        ensure_dir(path)
    return path


def queue_task_path(task: dict[str, Any]) -> Path:
    ts = int(float(task.get("received_ts") or time.time()) * 1000)
    task_id = safe_slug(str(task.get("task_id") or "task"), "task")
    return QUEUE_PENDING_DIR / f"{ts:013d}-{task_id}.json"


def get_default_project(config: dict[str, Any] | None, settings: dict[str, Any]) -> tuple[str, str]:
    project_id = str(settings["project"].get("defaultId") or "").strip()
    project_root = str(settings["project"].get("defaultRoot") or "").strip()
    if not project_id:
        workspace = ""
        if isinstance(config, dict):
            workspace = (
                str(config.get("agents", {}).get("defaults", {}).get("workspace") or "").strip()
            )
        project_id = safe_slug(Path(workspace).name if workspace else ROOT.name, "phase1-remote-dev")
    if not project_root:
        if isinstance(config, dict):
            project_root = (
                str(config.get("agents", {}).get("defaults", {}).get("workspace") or "").strip()
            )
    normalized_root = normalize_project_root(project_root or str(ROOT))
    return project_id, normalized_root or str(ROOT.resolve(strict=False))


def get_session_state(
    session_key: str,
    chat_id: str,
    channel: str,
    default_project_id: str,
    default_project_root: str,
) -> dict[str, Any]:
    ensure_runtime_layout()
    normalized_default_root = normalize_project_root(default_project_root) or managed_project_root(default_project_id)
    state = read_json(session_state_path(session_key), default=None)
    if isinstance(state, dict):
        state.setdefault("session_key", session_key)
        state.setdefault("chat_id", chat_id)
        state.setdefault("channel", channel)
        state.setdefault("current_project_id", default_project_id)
        state["current_project_root"] = normalize_project_root(str(state.get("current_project_root") or "")) or normalized_default_root
        state.setdefault("current_session_id", new_session_id())
        state.setdefault(
            "active_session_id",
            str(state.get("current_session_id") or "") if str(state.get("active_task_id") or "").strip() else "",
        )
        state.setdefault("recent_message_ids", [])
        state.setdefault("active_task_id", "")
        state.setdefault("last_task_id", "")
        state.setdefault("last_result", "")
        state.setdefault("last_progress", "")
        state.setdefault("last_reply_code", "")
        state.setdefault("last_failure_category", "")
        state.setdefault("last_task_finished_at", "")
        state.setdefault("last_inbound_at", "")
        return state
    return {
        "session_key": session_key,
        "chat_id": chat_id,
        "channel": channel,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_project_id": default_project_id,
        "current_project_root": normalized_default_root,
        "current_session_id": new_session_id(),
        "active_session_id": "",
        "recent_message_ids": [],
        "active_task_id": "",
        "last_task_id": "",
        "last_result": "",
        "last_progress": "",
        "last_reply_code": "",
        "last_failure_category": "",
        "last_task_finished_at": "",
        "last_inbound_at": "",
    }


def save_session_state(session_key: str, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(session_state_path(session_key), state)


def get_project_state(project_id: str, project_root: str, session_key: str, session_id: str) -> dict[str, Any]:
    normalized_root = normalize_project_root(project_root)
    state = read_project_state(project_id, normalized_root)
    if isinstance(state, dict):
        state["project_id"] = project_id
        state["project_root"] = normalized_root or normalize_project_root(str(state.get("project_root") or "")) or managed_project_root(project_id)
        state.setdefault("created_at", now_iso())
        state.setdefault("current_session_key", session_key)
        state.setdefault("current_session_id", session_id)
        state.setdefault("last_task_id", "")
        state.setdefault("last_result", "")
        state.setdefault("last_reply_code", "")
        state.setdefault("last_failure_category", "")
        state.setdefault("last_task_finished_at", "")
        return state
    return {
        "project_id": project_id,
        "project_root": normalized_root or managed_project_root(project_id),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_session_key": session_key,
        "current_session_id": session_id,
        "last_task_id": "",
        "last_result": "",
        "last_reply_code": "",
        "last_failure_category": "",
        "last_task_finished_at": "",
    }


def save_project_state(project_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(project_state_path(project_id, str(state.get("project_root") or "")), state)


def routing_session_id(state: dict[str, Any]) -> str:
    return str(state.get("current_session_id") or "").strip()


def active_session_id(state: dict[str, Any]) -> str:
    active_task = str(state.get("active_task_id") or "").strip()
    active_session = str(state.get("active_session_id") or "").strip()
    if active_task:
        return active_session or routing_session_id(state)
    return ""


def bind_active_session(
    state: dict[str, Any],
    session_id: str,
    task_id: str,
    project_id: str = "",
    project_root: str = "",
) -> None:
    session_id = session_id.strip()
    task_id = task_id.strip()
    current_routing_session = routing_session_id(state)
    if not current_routing_session or current_routing_session == session_id:
        state["current_session_id"] = session_id
        if project_id:
            state["current_project_id"] = project_id
        if project_root:
            normalized_root = normalize_project_root(project_root)
            if normalized_root:
                state["current_project_root"] = normalized_root
    state["active_task_id"] = task_id
    state["active_session_id"] = session_id


def release_active_session(state: dict[str, Any], session_id: str = "") -> None:
    session_id = session_id.strip()
    current_active_session = active_session_id(state) or str(state.get("active_session_id") or "").strip()
    if not session_id or not current_active_session or current_active_session == session_id:
        state["active_task_id"] = ""
        state["active_session_id"] = ""


def bind_running_task_state(
    *,
    session_key: str,
    chat_id: str,
    channel: str,
    default_project_id: str,
    default_project_root: str,
    project_id: str,
    project_root: str,
    session_id: str,
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with interprocess_lock(session_state_lock_name(session_key), timeout_seconds=15):
        session_state = get_session_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=channel,
            default_project_id=default_project_id,
            default_project_root=default_project_root,
        )
        bind_active_session(
            session_state,
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            project_root=project_root,
        )
        session_state["last_reply_code"] = "running"
        session_state["last_failure_category"] = ""
        save_session_state(session_key, session_state)

    with interprocess_lock(project_state_lock_name(project_id, project_root), timeout_seconds=15):
        project_state = get_project_state(project_id, project_root, session_key, session_id)
        project_state["current_session_key"] = session_key
        project_state["current_session_id"] = session_id
        project_state["last_task_id"] = task_id
        project_state["last_reply_code"] = "running"
        project_state["last_failure_category"] = ""
        save_project_state(project_id, project_state)
    return session_state, project_state


def merge_task_outcome_state(
    *,
    session_key: str,
    chat_id: str,
    channel: str,
    default_project_id: str,
    default_project_root: str,
    project_id: str,
    project_root: str,
    session_id: str,
    task_id: str,
    result_text: str,
    progress: str,
    reply_code: str = "",
    failure_category: str = "",
    finished_at: str = "",
    clear_active: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    finished_marker = finished_at or now_iso()
    with interprocess_lock(session_state_lock_name(session_key), timeout_seconds=15):
        session_state = get_session_state(
            session_key=session_key,
            chat_id=chat_id,
            channel=channel,
            default_project_id=default_project_id,
            default_project_root=default_project_root,
        )
        latest_active_task = str(session_state.get("active_task_id") or "").strip()
        latest_active_session = active_session_id(session_state)
        if clear_active and (latest_active_task == task_id or latest_active_session == session_id):
            release_active_session(session_state, session_id)
        session_state["last_task_id"] = task_id
        session_state["last_result"] = result_text
        session_state["last_progress"] = progress
        session_state["last_reply_code"] = reply_code
        session_state["last_failure_category"] = failure_category
        session_state["last_task_finished_at"] = finished_marker
        save_session_state(session_key, session_state)

    project_state: dict[str, Any] | None = None
    if project_id:
        with interprocess_lock(project_state_lock_name(project_id, project_root), timeout_seconds=15):
            project_state = get_project_state(project_id, project_root, session_key, session_id)
            project_state["last_task_id"] = task_id
            project_state["last_result"] = result_text
            project_state["last_reply_code"] = reply_code
            project_state["last_failure_category"] = failure_category
            project_state["last_task_finished_at"] = finished_marker
            save_project_state(project_id, project_state)
    return session_state, project_state


def message_id_marker_path(session_key: str, message_id: str) -> Path:
    composite = f"{session_key}|{message_id}"
    return MESSAGE_ID_DIR / f"{hash_key(composite, length=40)}.json"


def message_id_prune_state_path() -> Path:
    return CONTROL_DIR / "message-id-prune-state.json"


def prune_expired_message_id_markers(ttl_seconds: int = DEFAULT_MESSAGE_ID_TTL_SECONDS) -> None:
    ensure_runtime_layout()
    effective_ttl = max(int(ttl_seconds or 0), 60)
    now_ts = time.time()
    state_path = message_id_prune_state_path()
    state = read_json(state_path, default={}, allow_backup=False)
    try:
        last_started_ts = float((state or {}).get("last_started_ts") or 0.0)
    except (TypeError, ValueError, AttributeError):
        last_started_ts = 0.0
    if now_ts - last_started_ts < MESSAGE_ID_PRUNE_INTERVAL_SECONDS:
        return

    try:
        with interprocess_lock("message-id-prune", timeout_seconds=0.05, poll_seconds=0.05):
            state = read_json(state_path, default={}, allow_backup=False)
            try:
                last_started_ts = float((state or {}).get("last_started_ts") or 0.0)
            except (TypeError, ValueError, AttributeError):
                last_started_ts = 0.0
            if now_ts - last_started_ts < MESSAGE_ID_PRUNE_INTERVAL_SECONDS:
                return

            write_json(
                state_path,
                {
                    "last_started_at": now_iso(),
                    "last_started_ts": now_ts,
                    "ttl_seconds": effective_ttl,
                },
            )

            removed = 0
            scanned = 0
            cutoff_ts = now_ts - effective_ttl
            for marker_path in sorted(MESSAGE_ID_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime):
                if scanned >= MESSAGE_ID_PRUNE_BATCH_LIMIT:
                    break
                scanned += 1
                try:
                    marker_mtime = marker_path.stat().st_mtime
                except OSError:
                    continue
                if marker_mtime >= cutoff_ts:
                    continue
                marker_path.unlink(missing_ok=True)
                removed += 1

            write_json(
                state_path,
                {
                    "last_started_at": now_iso(),
                    "last_started_ts": now_ts,
                    "last_completed_at": now_iso(),
                    "last_completed_ts": time.time(),
                    "last_removed_count": removed,
                    "last_scanned_count": scanned,
                    "ttl_seconds": effective_ttl,
                },
            )
    except TimeoutError:
        return


def reserve_message_id(session_key: str, message_id: str, ttl_seconds: int = DEFAULT_MESSAGE_ID_TTL_SECONDS) -> bool:
    message_id = (message_id or "").strip()
    if not message_id:
        return False

    ensure_runtime_layout()
    prune_expired_message_id_markers(ttl_seconds)
    marker_path = message_id_marker_path(session_key, message_id)
    if marker_path.exists():
        age_seconds = time.time() - marker_path.stat().st_mtime
        if age_seconds <= max(ttl_seconds, 60):
            return True
        marker_path.unlink(missing_ok=True)

    try:
        fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return True

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "session_key": session_key,
                        "message_id": message_id,
                        "created_at": now_iso(),
                        "created_ts": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return False


def register_message_id(state: dict[str, Any], message_id: str, limit: int) -> bool:
    message_id = (message_id or "").strip()
    if not message_id:
        return False
    recent = [str(item) for item in state.get("recent_message_ids", []) if str(item).strip()]
    if message_id in recent:
        return True
    recent.append(message_id)
    if len(recent) > limit:
        recent = recent[-limit:]
    state["recent_message_ids"] = recent
    return False


def looks_like_remote_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_control_command(text: str) -> dict[str, Any]:
    clean = (text or "").strip()
    if not clean:
        return {"kind": "empty", "flush": False, "value": "", "request_text": ""}

    match = PROJECT_SWITCH_RE.match(clean)
    if match:
        return {
            "kind": "switch_project",
            "flush": True,
            "value": match.group(1).strip(),
            "request_text": clean,
        }

    match = NEW_TASK_RE.match(clean)
    if match:
        return {
            "kind": "new_task",
            "flush": True,
            "value": match.group(1).strip(),
            "request_text": match.group(1).strip(),
        }

    if RESET_SESSION_RE.match(clean):
        return {"kind": "reset_session", "flush": True, "value": "", "request_text": clean}
    if CONTINUE_RE.match(clean):
        return {"kind": "continue_current", "flush": True, "value": "", "request_text": clean}
    if SUMMARY_RE.match(clean):
        return {"kind": "summarize_current", "flush": True, "value": "", "request_text": clean}
    if STOP_RE.match(clean):
        return {"kind": "stop_current", "flush": True, "value": "", "request_text": clean}
    match = PROJECT_HISTORY_RE.match(clean)
    if match:
        return {
            "kind": "project_history",
            "flush": True,
            "value": (match.group(1) or "").strip(),
            "request_text": clean,
        }
    match = LIST_PATH_RE.match(clean)
    if match:
        return {
            "kind": "browse_path",
            "flush": True,
            "value": match.group(1).strip(),
            "request_text": clean,
        }
    match = READ_FILE_RE.match(clean)
    if match:
        return {
            "kind": "read_file",
            "flush": True,
            "value": match.group(1).strip(),
            "request_text": clean,
        }
    match = SEND_FILE_RE.match(clean)
    if match:
        return {
            "kind": "send_file",
            "flush": True,
            "value": match.group(1).strip(),
            "request_text": clean,
        }

    return {"kind": "none", "flush": False, "value": "", "request_text": clean}


def normalize_qq_text(text: str) -> str:
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    clean = re.sub(r"(?m)^\s*```[^\n]*\n?", "", clean)
    clean = re.sub(r"(?m)^\s*```\s*$", "", clean)
    clean = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", clean)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"\*\*(.*?)\*\*", r"\1", clean)
    clean = re.sub(r"__(.*?)__", r"\1", clean)
    clean = re.sub(r"(?m)^\s*>\s*", "", clean)
    clean = re.sub(r"(?m)^\s*[-*+]\s+", "- ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def normalize_search_text(text: str) -> str:
    clean = normalize_qq_text(text).lower()
    clean = clean.replace("→", " ").replace("->", " ").replace("/", " ").replace("\\", " ")
    clean = re.sub(r"[^\w\u4e00-\u9fff]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def extract_search_terms(text: str, limit: int = 32) -> list[str]:
    normalized = normalize_search_text(text)
    if not normalized:
        return []

    stop_terms = {
        "一个",
        "一下",
        "这个",
        "那个",
        "然后",
        "现在",
        "之前",
        "以前",
        "就是",
        "里面",
        "上面",
        "上边",
        "下面",
        "一下子",
        "有没有",
        "是不是",
        "可以",
        "能够",
        "给我",
        "帮我",
        "看看",
        "找找",
        "查查",
        "一下子",
    }

    terms: list[str] = []
    seen: set[str] = set()
    for chunk in normalized.split():
        if re.fullmatch(r"[a-z0-9]{2,}", chunk):
            if chunk not in stop_terms and chunk not in seen:
                seen.add(chunk)
                terms.append(chunk)
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            continue
        if 2 <= len(chunk) <= 8 and chunk not in stop_terms and chunk not in seen:
            seen.add(chunk)
            terms.append(chunk)
        max_window = min(4, len(chunk))
        for width in range(2, max_window + 1):
            for index in range(0, len(chunk) - width + 1):
                piece = chunk[index : index + width]
                if piece in stop_terms or piece in seen:
                    continue
                seen.add(piece)
                terms.append(piece)
                if len(terms) >= limit:
                    return terms
    return terms[:limit]


def detect_freeform_request_intent(text: str) -> dict[str, Any] | None:
    normalized = normalize_search_text(text)
    if not normalized:
        return None

    history_markers = ("之前", "以前", "历史", "记录", "做过", "项目", "找得到", "找不找得到", "还记得")
    tool_markers = ("claude", "codex", "nanobot", "qq", "agent", "ai")
    analysis_markers = ("优化空间", "继续优化", "还能优化", "改进空间", "继续改", "还能改")
    if any(marker in normalized for marker in history_markers) and any(marker in normalized for marker in tool_markers):
        if any(marker in normalized for marker in analysis_markers):
            return {"kind": "history_analysis", "query": str(text or "").strip()}
        return {"kind": "project_history", "query": str(text or "").strip()}

    search_verbs = (
        "找找",
        "找一下",
        "找出来",
        "找一找",
        "看看",
        "搜一下",
        "搜索",
        "查一下",
        "查找",
        "有没有",
        "应该有",
        "是不是有",
        "保存着",
        "存着",
    )
    computer_markers = ("电脑", "本地", "硬盘", "磁盘", "桌面", "下载", "全盘", "整个电脑", "我电脑", "手机")
    target_markers = ("文件", "文件夹", "目录", "图片", "照片", "截图", "屏幕截图", "简历", "pdf", "文档", "英文版")
    send_markers = (
        "发给我",
        "发送给我",
        "发到手机",
        "发送到手机",
        "发到我手机",
        "发送到我手机",
        "发我看看",
        "给我看看",
        "传给我",
        "传到手机",
        "发到qq",
        "发送到qq",
        "回传",
    )
    location_markers = (
        "应该在",
        "下载文件夹",
        "下载目录",
        "桌面上",
        "文件夹里",
        "文件夹里面",
        "目录里",
        "目录里面",
        "路径",
        "里面写着",
        "里面的是",
        "类似",
        "最近一周",
        "更新时间",
        "应该有一个文件夹",
        "保存着一些",
        "存着一些",
    )
    drive_hint = re.search(r"[a-zA-Z]\s*盘|[a-zA-Z]:[\\/]", str(text or "")) is not None
    if any(marker in normalized for marker in computer_markers) and (
        any(marker in normalized for marker in target_markers) or any(marker in normalized for marker in send_markers)
    ):
        if (
            any(marker in normalized for marker in search_verbs)
            or any(marker in normalized for marker in send_markers)
            or any(marker in normalized for marker in location_markers)
            or drive_hint
        ):
            return {
                "kind": "ai_file_search",
                "query": str(text or "").strip(),
                "wants_send": any(marker in normalized for marker in send_markers),
            }
    return None


def format_file_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(size_bytes, 0))
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def resolve_user_visible_path(path_text: str, base_root: str = "") -> Path:
    raw = str(path_text or "").strip().strip("\"'")
    if not raw:
        raise ValueError("missing-path")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        base_path = Path(normalize_project_root(base_root) or str(ROOT))
        candidate = base_path / candidate
    return Path(normalize_project_root(str(candidate)))


def list_local_drive_roots(extra_roots: list[str] | None = None) -> list[str]:
    roots = [str(item) for item in (extra_roots or []) if str(item).strip()]
    if not roots:
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                candidate = f"{letter}:\\"
                if Path(candidate).exists():
                    roots.append(candidate)
        else:
            roots.append("/")

    normalized_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = normalize_project_root(root)
        if not normalized:
            continue
        key = stable_text_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        normalized_roots.append(normalized)
    return normalized_roots


def is_path_within_any_root(path: str | Path, allowed_roots: list[str] | None = None) -> bool:
    normalized_path = normalize_project_root(path)
    if not normalized_path or not allowed_roots:
        return False
    return any(is_path_within(normalized_path, root) for root in allowed_roots)


def build_project_history_reply(query: str = "", limit: int = 8) -> str:
    query_text = str(query or "").strip()
    query_phrase = normalize_search_text(query_text)
    query_terms = extract_search_terms(query_text)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    task_dirs = sorted(
        (path for path in TASKS_DIR.iterdir() if path.is_dir()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ) if TASKS_DIR.exists() else []

    for task_dir in task_dirs:
        status = read_json(task_dir / "status.json", default=None)
        task_payload = read_json(task_dir / "task.json", default=None)
        if not isinstance(status, dict) and not isinstance(task_payload, dict):
            continue

        status = status if isinstance(status, dict) else {}
        task_payload = task_payload if isinstance(task_payload, dict) else {}
        project_id = str(status.get("project_id") or task_payload.get("project_id") or "").strip()
        project_root = normalize_project_root(str(status.get("project_root") or task_payload.get("project_root") or ""))
        if not project_id and not project_root:
            continue

        latest_at = str(
            status.get("finished_at")
            or status.get("started_at")
            or task_payload.get("received_at")
            or ""
        ).strip()
        latest_task = str(
            status.get("task_name")
            or task_payload.get("user_request")
            or task_payload.get("task_id")
            or task_dir.name
        ).strip()
        latest_result = str(status.get("result") or status.get("error") or "").strip()
        latest_phase = str(status.get("phase") or "").strip() or "unknown"
        search_blob = normalize_search_text(
            "\n".join(
                [
                    project_id,
                    project_root,
                    latest_task,
                    latest_result,
                    str(task_payload.get("user_request") or ""),
                    str(task_payload.get("task_id") or ""),
                ]
            )
        )
        match_score = 0
        if query_phrase:
            if query_phrase in search_blob:
                match_score += max(8, len(query_terms) * 2)
            for term in query_terms:
                if term in search_blob:
                    match_score += 2
        else:
            match_score = 1
        if query_text and match_score <= 0:
            continue

        key = (project_id or "unknown", project_root)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "project_id": project_id or "unknown",
                "project_root": project_root or "未记录",
                "task_count": 1,
                "latest_at": latest_at,
                "latest_task": latest_task,
                "latest_result": latest_result,
                "latest_phase": latest_phase,
                "match_score": match_score,
            }
            continue

        entry["task_count"] += 1
        entry["match_score"] = max(int(entry.get("match_score") or 0), match_score)
        if latest_at >= str(entry.get("latest_at") or ""):
            entry["latest_at"] = latest_at
            entry["latest_task"] = latest_task
            entry["latest_result"] = latest_result
            entry["latest_phase"] = latest_phase

    items = sorted(
        grouped.values(),
        key=lambda item: (int(item.get("match_score") or 0), str(item.get("latest_at") or "")),
        reverse=True,
    )[: max(limit, 1)]

    if not items:
        if query_text:
            return f"没有找到和“{query_text}”相关的历史项目。"
        return "还没有可供查看的历史项目记录。"

    lines = ["历史项目"]
    if query_text:
        lines[0] += f"（筛选：{query_text}）"

    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item['project_id']}")
        lines.append(f"   根目录：{item['project_root']}")
        lines.append(f"   最近时间：{item.get('latest_at') or '未记录'}")
        lines.append(f"   最近状态：{item.get('latest_phase') or 'unknown'}")
        lines.append(f"   累计任务数：{item.get('task_count') or 0}")
        lines.append(f"   最近任务：{truncate(str(item.get('latest_task') or '未记录'), 120)}")
        latest_result = normalize_qq_text(str(item.get("latest_result") or ""))
        if latest_result:
            lines.append(f"   最近结果：{truncate(first_nonempty_line(latest_result, latest_result), 120)}")

    return "\n".join(lines)


def build_project_history_context(query: str = "", limit: int = 5) -> str:
    return build_project_history_reply(query, limit=limit)


def describe_local_path(path_text: str, base_root: str = "", limit: int = 24) -> str:
    try:
        path = resolve_user_visible_path(path_text, base_root=base_root)
    except ValueError:
        return "没有收到可用的路径。请用“查看目录：绝对路径”或“查看目录：相对路径”。"

    if not path.exists():
        return f"路径不存在：{path}"

    if path.is_file():
        stat = path.stat()
        return "\n".join(
            [
                "路径信息",
                f"1. 类型：文件",
                f"2. 路径：{path}",
                f"3. 大小：{format_file_size(stat.st_size)}",
                f"4. 修改时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}",
                "5. 如果要看内容，请发送“查看文件：同一路径”。",
                "6. 如果要直接回传到手机，请发送“发送文件：同一路径”。",
            ]
        )

    try:
        entries = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except PermissionError:
        return f"没有权限读取这个目录：{path}"

    dir_count = sum(1 for item in entries if item.is_dir())
    file_count = len(entries) - dir_count
    shown = entries[: max(limit, 1)]
    lines = [
        "目录内容",
        f"1. 路径：{path}",
        f"2. 统计：共 {len(entries)} 项，目录 {dir_count} 个，文件 {file_count} 个",
        f"3. 前 {len(shown)} 项：",
    ]
    for index, item in enumerate(shown, 1):
        tag = "DIR" if item.is_dir() else "FILE"
        suffix = ""
        if item.is_file():
            try:
                suffix = f" ({format_file_size(item.stat().st_size)})"
            except OSError:
                suffix = ""
        lines.append(f"   {index}. [{tag}] {item.name}{suffix}")
    if len(entries) > len(shown):
        lines.append(f"4. 还有 {len(entries) - len(shown)} 项未展开。")
    return "\n".join(lines)


def preview_local_file(path_text: str, base_root: str = "", max_bytes: int = 32 * 1024, max_lines: int = 80) -> str:
    try:
        path = resolve_user_visible_path(path_text, base_root=base_root)
    except ValueError:
        return "没有收到可用的文件路径。请用“查看文件：绝对路径”或“查看文件：相对路径”。"

    if not path.exists():
        return f"文件不存在：{path}"
    if not path.is_file():
        return f"这不是文件：{path}"

    file_size = path.stat().st_size
    sample = path.read_bytes()[: max_bytes]
    if b"\x00" in sample:
        return "\n".join(
            [
                "文件预览",
                f"1. 路径：{path}",
                f"2. 类型：二进制或图片文件",
                f"3. 大小：{format_file_size(file_size)}",
                "4. 这种文件不适合直接文本预览；如果要发到手机，请使用“发送文件：同一路径”。",
            ]
        )

    decoded = None
    encoding_used = ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
        try:
            decoded = sample.decode(encoding)
            encoding_used = encoding
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        decoded = sample.decode("utf-8", errors="replace")
        encoding_used = "utf-8/replace"

    lines = decoded.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    preview_lines = lines[: max_lines]
    output = [
        "文件预览",
        f"1. 路径：{path}",
        f"2. 大小：{format_file_size(file_size)}",
        f"3. 编码：{encoding_used}",
        "4. 内容：",
    ]
    if not preview_lines:
        output.append("   1. 这个文件是空的。")
    else:
        for index, line in enumerate(preview_lines, 1):
            output.append(f"   {index}. {line}")
    if file_size > max_bytes or len(lines) > len(preview_lines):
        output.append("5. 这只是部分预览；如果要完整带回手机，请使用“发送文件：同一路径”。")
    return "\n".join(output)


def normalize_allowed_attachment_roots(project_root: str, extra_roots: list[str] | None = None) -> list[str]:
    roots = [project_root, str(ROOT / "runtime" / "media")]
    roots.extend(extra_roots or [])
    normalized_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = normalize_project_root(root)
        if not normalized:
            continue
        key = stable_text_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        normalized_roots.append(normalized)
    return normalized_roots


def is_safe_local_attachment_path(path: Path, allowed_roots: list[str]) -> bool:
    if not path.is_absolute():
        return False
    return is_path_within_any_root(path, allowed_roots)


def _guess_attachment_kind_from_name(name: str) -> str:
    return "image" if Path(name).suffix.lower() in IMAGE_SUFFIXES else "file"


def normalize_attachment_item(item: Any, allowed_roots: list[str] | None = None) -> dict[str, Any] | None:
    raw_path = ""
    source_url = ""
    meta: dict[str, Any] = {}
    if isinstance(item, str):
        raw_path = item.strip()
    elif isinstance(item, dict):
        meta = dict(item)
        source_url = str(item.get("url") or "").strip()
        for field_name in ATTACHMENT_PATH_FIELDS:
            candidate = str(item.get(field_name) or "").strip()
            if candidate:
                raw_path = candidate
                break

    if raw_path and not source_url and looks_like_remote_url(raw_path):
        source_url = raw_path
        raw_path = ""

    if raw_path:
        normalized_path = normalize_project_root(raw_path)
        if not normalized_path:
            return None
        path = Path(normalized_path)
        if allowed_roots and not is_safe_local_attachment_path(path, allowed_roots):
            raw_path = ""
        else:
            exists = path.is_file()
            size_bytes = path.stat().st_size if exists else meta.get("size_bytes") or meta.get("size")
            return {
                "path": str(path),
                "name": str(meta.get("name") or path.name or path.stem or "attachment"),
                "kind": str(meta.get("kind") or _guess_attachment_kind_from_name(path.name)),
                "exists": bool(exists),
                "size_bytes": int(size_bytes) if str(size_bytes or "").isdigit() else size_bytes,
                "source_url": source_url,
                "location": "local",
            }

    if source_url:
        parsed = urllib.parse.urlparse(source_url)
        source_name = Path(parsed.path).name or str(meta.get("name") or "attachment")
        return {
            "path": "",
            "name": str(meta.get("name") or source_name),
            "kind": str(meta.get("kind") or _guess_attachment_kind_from_name(source_name)),
            "exists": False,
            "size_bytes": meta.get("size_bytes") or meta.get("size"),
            "source_url": source_url,
            "location": "remote",
        }
    return None


def extract_paths_from_text(text: str) -> list[str]:
    # Untrusted QQ text must never auto-promote arbitrary local Windows paths
    # into attachments. Attachments must arrive through explicit metadata.
    return []


def extract_attachments(raw_task: dict[str, Any], allowed_roots: list[str] | None = None) -> list[dict[str, Any]]:
    sources: list[Any] = []
    metadata = raw_task.get("metadata") or {}
    for key in ("attachments", "media"):
        value = raw_task.get(key)
        if isinstance(value, list):
            sources.extend(value)
        value = metadata.get(key)
        if isinstance(value, list):
            sources.extend(value)

    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sources:
        normalized = normalize_attachment_item(item, allowed_roots=allowed_roots)
        if not normalized:
            continue
        key = (str(normalized.get("path") or "") or str(normalized.get("source_url") or "")).lower()
        if key in seen:
            continue
        seen.add(key)
        attachments.append(normalized)
    return attachments


def summarize_attachments(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return "无附件。"
    lines = []
    for index, item in enumerate(attachments, 1):
        size_note = ""
        if isinstance(item.get("size_bytes"), int):
            size_note = f" ({item['size_bytes']} bytes)"
        location = str(item.get("path") or item.get("source_url") or "")
        exists_note = ""
        if item.get("location") == "local" and not item.get("exists"):
            exists_note = " [文件当前不存在]"
        if item.get("location") == "remote":
            exists_note = " [remote-url]"
        lines.append(f"{index}. [{item.get('kind', 'file')}] {item.get('name', '')}: {location}{size_note}{exists_note}")
    return "\n".join(lines)


def append_session_event(session_key: str, session_id: str, payload: dict[str, Any]) -> None:
    append_jsonl(session_log_path(session_key, session_id), payload)


def recent_session_events(session_key: str, session_id: str, limit: int) -> list[dict[str, Any]]:
    path = session_log_path(session_key, session_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-max(limit * 2, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(payload)
    return events[-limit:]


def queue_pending_files() -> list[Path]:
    ensure_runtime_layout()
    return sorted(QUEUE_PENDING_DIR.glob("*.json"), key=lambda item: item.name)


def queue_processing_files() -> list[Path]:
    ensure_runtime_layout()
    return sorted(QUEUE_PROCESSING_DIR.glob("*.json"), key=lambda item: item.name)


def queue_depth() -> int:
    return len(queue_pending_files())


def same_task_bucket(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("session_key") or "") == str(right.get("session_key") or "")
        and str(left.get("session_id") or "") == str(right.get("session_id") or "")
        and str(left.get("project_id") or "") == str(right.get("project_id") or "")
        and normalize_project_root(str(left.get("project_root") or "")) == normalize_project_root(str(right.get("project_root") or ""))
    )


def queue_depth_for_session(session_key: str, session_id: str, project_id: str, project_root: str = "") -> int:
    probe = {
        "session_key": session_key,
        "session_id": session_id,
        "project_id": project_id,
        "project_root": project_root,
    }
    count = 0
    for path in queue_pending_files():
        payload = read_json(path, default=None)
        if isinstance(payload, dict) and same_task_bucket(probe, payload):
            count += 1
    return count


def claim_next_pending_task() -> tuple[dict[str, Any], Path] | None:
    for path in queue_pending_files():
        processing_path = QUEUE_PROCESSING_DIR / path.name
        try:
            path.replace(processing_path)
        except FileNotFoundError:
            continue
        payload = read_json(processing_path, default=None)
        if isinstance(payload, dict):
            return payload, processing_path
        processing_path.unlink(missing_ok=True)
    return None


def restore_queue_file(source_path: Path, move: bool = True) -> Path | None:
    ensure_runtime_layout()
    if not source_path.exists():
        return None

    target = QUEUE_PENDING_DIR / source_path.name
    if target.exists():
        if move:
            source_path.unlink(missing_ok=True)
        return target

    try:
        if move:
            source_path.replace(target)
        else:
            target.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        try:
            target.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            return None
        if move:
            source_path.unlink(missing_ok=True)
    return target


def claim_matching_pending_tasks(seed_task: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
    claimed: list[tuple[dict[str, Any], Path]] = []
    for path in queue_pending_files():
        try:
            candidate = read_json(path, default=None)
        except Exception:
            candidate = None
        if not isinstance(candidate, dict):
            continue
        if not same_task_bucket(seed_task, candidate):
            continue
        processing_path = QUEUE_PROCESSING_DIR / path.name
        try:
            path.replace(processing_path)
        except FileNotFoundError:
            continue
        payload = read_json(processing_path, default=None)
        if isinstance(payload, dict):
            claimed.append((payload, processing_path))
            if payload.get("routing_mode") == "flush" or (payload.get("attachments") or []):
                break
    claimed.sort(key=lambda item: item[1].name)
    return claimed


def archive_claimed_queue_file(processing_path: Path, task_dir: Path) -> None:
    inbound_dir = ensure_dir(task_dir / "inbound")
    target = inbound_dir / processing_path.name
    try:
        processing_path.replace(target)
    except Exception:
        try:
            target.write_text(processing_path.read_text(encoding="utf-8"), encoding="utf-8")
        finally:
            processing_path.unlink(missing_ok=True)


def stop_request_path(session_key: str, session_id: str) -> Path:
    session_bucket = safe_bucket_name(session_key, "session")
    session_id_bucket = safe_bucket_name(session_id, "session")
    return STOP_REQUEST_DIR / f"{session_bucket}--{session_id_bucket}.json"


def create_stop_request(session_key: str, session_id: str, reason: str, requested_by: str) -> Path:
    path = stop_request_path(session_key, session_id)
    write_json(
        path,
        {
            "session_key": session_key,
            "session_id": session_id,
            "reason": reason,
            "requested_by": requested_by,
            "requested_at": now_iso(),
        },
    )
    return path


def clear_stop_request(session_key: str, session_id: str) -> None:
    path = stop_request_path(session_key, session_id)
    path.unlink(missing_ok=True)
    json_backup_path(path).unlink(missing_ok=True)


def read_stop_request(session_key: str, session_id: str) -> dict[str, Any] | None:
    payload = read_json(stop_request_path(session_key, session_id), default=None, allow_backup=False)
    return payload if isinstance(payload, dict) else None


def format_progress(stage: str, task_name: str, detail: str) -> str:
    return f"[{stage}] {task_name}\n{detail}".strip()


def render_active_task(
    task_name: str,
    status: str,
    owner: str,
    project_id: str,
    session_id: str,
    next_checkpoint: str,
    started_at: str,
    last_updated: str,
) -> str:
    return "\n".join(
        [
            "# Active Task",
            "",
            f"- status: {status}",
            f"- task: {task_name}",
            f"- owner: {owner}",
            f"- project_id: {project_id}",
            f"- session_id: {session_id}",
            f"- started: {started_at}",
            f"- last_updated: {last_updated}",
            f"- next_checkpoint: {next_checkpoint}",
        ]
    )


def read_active_task_started_at() -> str:
    if not ACTIVE_TASK_FILE.exists():
        return ""
    try:
        for line in ACTIVE_TASK_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("- started:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        return ""
    return ""


def update_runtime_state(
    task_name: str,
    status: str,
    progress: str,
    owner: str,
    project_id: str,
    session_id: str,
    heartbeat_interval_seconds: int,
    started_at: str | None = None,
) -> None:
    last_updated = now_iso()
    effective_started_at = started_at or read_active_task_started_at() or last_updated
    checkpoint = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.localtime(time.time() + heartbeat_interval_seconds),
    )
    write_text(
        ACTIVE_TASK_FILE,
        render_active_task(task_name, status, owner, project_id, session_id, checkpoint, effective_started_at, last_updated),
    )
    write_text(QQ_PROGRESS_FILE, progress.strip() + "\n")


def truncate(text: str, limit: int = 240) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def first_nonempty_line(text: str, fallback: str = "") -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return fallback


def split_qq_text(text: str, limit: int = 1200) -> list[str]:
    clean = normalize_qq_text(text)
    if not clean:
        return []
    if len(clean) <= limit:
        return [clean]
    pieces: list[str] = []
    current = ""
    for paragraph in clean.splitlines():
        paragraph = paragraph.rstrip()
        candidate = paragraph if not current else current + "\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        while len(paragraph) > limit:
            pieces.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        pieces.append(current)
    return pieces


def guess_file_type(path: Path) -> int:
    return 1 if path.suffix.lower() in IMAGE_SUFFIXES else 4


def collect_allowed_artifact_roots(
    task_dir: Path,
    project_root: str,
    settings: dict[str, Any],
    extra_roots: list[str] | None = None,
) -> list[str]:
    roots = [str(task_dir / "artifacts")]
    if project_root:
        roots.append(str(project_execution_dir(project_root) / ".phase1-artifacts"))
    configured_extra_roots = settings.get("artifacts", {}).get("allowedRoots", []) if isinstance(settings.get("artifacts"), dict) else []
    if isinstance(configured_extra_roots, list):
        roots.extend(str(item) for item in configured_extra_roots if str(item).strip())
    roots.extend(str(item) for item in (extra_roots or []) if str(item).strip())

    normalized_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = normalize_project_root(root)
        if not normalized:
            continue
        key = stable_text_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        normalized_roots.append(normalized)
    return normalized_roots


def collect_artifact_payload(
    task_dir: Path,
    project_root: str,
    settings: dict[str, Any],
    extra_allowed_roots: list[str] | None = None,
) -> dict[str, list[str]]:
    payload = read_json(ARTIFACTS_FILE, default={"files": [], "urls": [], "notes": []}) or {}
    notes = [str(item).strip() for item in payload.get("notes", []) if str(item).strip()]
    urls = [str(item).strip() for item in payload.get("urls", []) if str(item).strip()]
    files: list[str] = []
    allowed_roots = collect_allowed_artifact_roots(task_dir, project_root, settings, extra_roots=extra_allowed_roots)
    max_single_file_bytes = int(settings["artifacts"].get("maxSingleFileBytes") or 25 * 1024 * 1024)

    for item in payload.get("files", []):
        normalized = normalize_project_root(str(item))
        if not normalized:
            continue
        path = Path(normalized)
        display_name = path.name or "artifact"
        if not path.exists():
            notes.append(f"未找到产物文件，已忽略：{display_name}")
            continue
        if not path.is_file():
            notes.append(f"产物不是文件，已忽略：{display_name}")
            continue
        if not any(is_path_within(path, root) for root in allowed_roots):
            notes.append(f"产物不在允许目录内，已忽略：{display_name}")
            continue
        if path.stat().st_size > max_single_file_bytes:
            notes.append(f"单个产物文件过大，已忽略：{display_name}")
            continue
        files.append(str(path))
    return {"files": files, "urls": urls, "notes": notes}


def estimate_base64_size(size_bytes: int) -> int:
    normalized = max(int(size_bytes or 0), 0)
    return ((normalized + 2) // 3) * 4


def qq_upload_size_allowed(size_bytes: int, settings: dict[str, Any]) -> bool:
    artifacts_cfg = settings.get("artifacts") if isinstance(settings.get("artifacts"), dict) else {}
    max_upload_bytes = int(artifacts_cfg.get("qqMaxUploadBytes") or 25 * 1024 * 1024)
    # Leave a little room for JSON envelope fields around the base64 payload.
    return estimate_base64_size(size_bytes) + 4096 <= max_upload_bytes


def int_setting(mapping: dict[str, Any], key: str, default: int) -> int:
    value = mapping.get(key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_unique_archive_name(path: Path, used_names: set[str]) -> str:
    parts = [part for part in path.parts if part and part != path.anchor]
    for depth in range(1, len(parts) + 1):
        candidate = "/".join(parts[-depth:])
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate

    candidate = f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}"
    used_names.add(candidate)
    return candidate


def package_artifacts_for_qq(
    task_dir: Path,
    task_id: str,
    settings: dict[str, Any],
    project_root: str = "",
    extra_allowed_roots: list[str] | None = None,
) -> dict[str, list[str]]:
    payload = collect_artifact_payload(task_dir, project_root, settings, extra_allowed_roots=extra_allowed_roots)
    notes = list(payload["notes"])
    files: list[Path] = []
    for item in payload["files"]:
        path = Path(item)
        if not path.is_file():
            continue
        if not qq_upload_size_allowed(path.stat().st_size, settings):
            notes.append(f"文件超过 QQ 直传限制，已忽略：{path.name}")
            continue
        files.append(path)
    if not files:
        return {"files": [], "urls": payload["urls"], "notes": notes}

    artifacts_cfg = settings.get("artifacts") if isinstance(settings.get("artifacts"), dict) else {}
    prefer_direct_send = bool(artifacts_cfg.get("preferDirectSend", True))
    max_files = int_setting(artifacts_cfg, "maxFiles", 0)
    max_total_bytes = int_setting(artifacts_cfg, "maxTotalBytes", 80 * 1024 * 1024)
    max_single_file_bytes = int_setting(artifacts_cfg, "maxSingleFileBytes", 25 * 1024 * 1024)
    zip_threshold = int_setting(artifacts_cfg, "zipThreshold", 0)
    direct_send_notice_threshold = int_setting(artifacts_cfg, "directSendNoticeThreshold", 8)
    total_bytes = sum(path.stat().st_size for path in files)

    if prefer_direct_send:
        if direct_send_notice_threshold > 0 and len(files) >= direct_send_notice_threshold:
            notes.append(f"本轮共有 {len(files)} 个文件，将按顺序直接回传到 QQ，不再优先强制打包。")
        return {
            "files": [str(path) for path in files],
            "urls": payload["urls"],
            "notes": notes,
        }

    exceeds_max_files = max_files > 0 and len(files) > max_files
    exceeds_max_total = max_total_bytes > 0 and total_bytes > max_total_bytes
    exceeds_zip_threshold = zip_threshold > 0 and len(files) >= zip_threshold

    if not exceeds_max_files and not exceeds_max_total and not exceeds_zip_threshold:
        return {
            "files": [str(path) for path in files],
            "urls": payload["urls"],
            "notes": notes,
        }

    bundle_dir = ensure_dir(task_dir / "artifacts")
    zip_name = f"{safe_slug(str(artifacts_cfg.get('zipNamePrefix') or 'phase1-artifacts'))}-{safe_slug(task_id, 'task')}.zip"
    zip_path = bundle_dir / zip_name
    used_archive_names: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=build_unique_archive_name(path, used_archive_names))

    zip_size = zip_path.stat().st_size if zip_path.exists() else 0
    if zip_size > max_single_file_bytes or zip_size > max_total_bytes or not qq_upload_size_allowed(zip_size, settings):
        zip_path.unlink(missing_ok=True)
        notes.append(f"打包后的压缩包超过 QQ 直传限制，已忽略：{zip_name}")
        return {
            "files": [],
            "urls": payload["urls"],
            "notes": notes,
        }

    notes.append(
        f"本轮产物数量或总体积超出直发阈值，已自动打包为 {zip_path}。"
    )
    return {
        "files": [str(zip_path)],
        "urls": payload["urls"],
        "notes": notes,
    }


def describe_session_summary(session_state: dict[str, Any], project_state: dict[str, Any] | None, queue_count: int, active_task_id: str) -> str:
    project_id = str(session_state.get("current_project_id") or "未设置")
    session_id = str(session_state.get("current_session_id") or "未设置")
    running_session_id = active_session_id(session_state)
    last_task = str(session_state.get("last_task_id") or "暂无")
    lines = [
        f"当前项目：{project_id}",
        f"当前会话：{session_id}",
    ]
    if running_session_id and running_session_id != session_id:
        lines.append(f"运行中会话：{running_session_id}")
    lines.extend(
        [
        f"活跃任务：{active_task_id or '无'}",
        f"队列条目：{queue_count}",
        f"最近任务：{last_task}",
        ]
    )
    if project_state and project_state.get("project_root"):
        lines.append(f"项目根目录：{project_state['project_root']}")
    last_result = str(session_state.get("last_result") or "").strip()
    if last_result:
        lines.append(f"最近结果：{truncate(last_result, 120)}")
    return "\n".join(lines)

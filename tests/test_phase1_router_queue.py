from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import phase1_router_queue as router
import phase1_runtime as runtime


class RouterSandbox:
    PATH_ATTRS = (
        "ROOT",
        "RUNTIME_DIR",
        "TASKS_DIR",
        "QUEUE_DIR",
        "QUEUE_PENDING_DIR",
        "QUEUE_PROCESSING_DIR",
        "PROJECTS_DIR",
        "MANAGED_WORKSPACES_DIR",
        "SESSION_RUNTIME_DIR",
        "LOGS_DIR",
        "ROUTER_LOG_DIR",
        "TASK_LOG_DIR",
        "CONTROL_DIR",
        "STOP_REQUEST_DIR",
        "LOCKS_DIR",
        "MESSAGE_ID_DIR",
        "TMP_DIR",
        "ADMIN_INBOX_DIR",
        "ADMIN_TASKS_DIR",
        "ACTIVE_TASK_FILE",
        "QQ_PROGRESS_FILE",
        "ARTIFACTS_FILE",
        "WORKER_LOCK_FILE",
        "ADMIN_LOCK_FILE",
    )

    def __init__(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="phase1-router-test-")
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir(parents=True, exist_ok=True)
        runtime_base = self.root / "runtime"
        self.replacements = {
            "ROOT": self.root,
            "RUNTIME_DIR": runtime_base,
            "TASKS_DIR": runtime_base / "tasks",
            "QUEUE_DIR": runtime_base / "queue",
            "QUEUE_PENDING_DIR": runtime_base / "queue" / "pending",
            "QUEUE_PROCESSING_DIR": runtime_base / "queue" / "processing",
            "PROJECTS_DIR": runtime_base / "projects",
            "MANAGED_WORKSPACES_DIR": self.root / "workspace",
            "SESSION_RUNTIME_DIR": runtime_base / "sessions",
            "LOGS_DIR": runtime_base / "logs",
            "ROUTER_LOG_DIR": runtime_base / "logs" / "router",
            "TASK_LOG_DIR": runtime_base / "logs" / "tasks",
            "CONTROL_DIR": runtime_base / "control",
            "STOP_REQUEST_DIR": runtime_base / "control" / "stop",
            "LOCKS_DIR": runtime_base / "control" / "locks",
            "MESSAGE_ID_DIR": runtime_base / "control" / "message-ids",
            "TMP_DIR": runtime_base / "tmp",
            "ADMIN_INBOX_DIR": runtime_base / "admin-inbox",
            "ADMIN_TASKS_DIR": runtime_base / "admin-tasks",
            "ACTIVE_TASK_FILE": runtime_base / "ACTIVE_TASK.md",
            "QQ_PROGRESS_FILE": runtime_base / "QQ_PROGRESS.md",
            "ARTIFACTS_FILE": runtime_base / "LAST_ARTIFACTS.json",
            "WORKER_LOCK_FILE": runtime_base / "current-worker.json",
            "ADMIN_LOCK_FILE": runtime_base / "current-admin-relay.json",
        }
        self.runtime_originals: dict[str, Path] = {}
        self.router_originals: dict[str, Path] = {}

    def __enter__(self) -> "RouterSandbox":
        for attr in self.PATH_ATTRS:
            self.runtime_originals[attr] = getattr(runtime, attr)
            setattr(runtime, attr, self.replacements[attr])
            if hasattr(router, attr):
                self.router_originals[attr] = getattr(router, attr)
                setattr(router, attr, self.replacements[attr])
        runtime.ensure_runtime_layout()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for attr, original in self.runtime_originals.items():
            setattr(runtime, attr, original)
        for attr, original in self.router_originals.items():
            setattr(router, attr, original)
        self.temp_dir.cleanup()


class Phase1RouterQueueTests(unittest.TestCase):
    def test_resolve_session_key_binds_to_chat(self) -> None:
        self.assertEqual(router.resolve_session_key("qq", "chat-1", "sender-1", "qq:chat-1"), "qq:chat-1")
        self.assertEqual(router.resolve_session_key("qq", "chat-1", "sender-1", "qq:other"), "qq:chat-1")
        self.assertEqual(router.resolve_session_key("qq", "chat-1", "sender-1", "smtp:chat-1"), "qq:chat-1")
        self.assertEqual(router.resolve_session_key("qq", "", "sender-1", "qq:other"), "qq:sender-1")
        self.assertEqual(router.resolve_session_key("qq", "", "", "qq:other", "msg-1"), "qq:msg:msg-1")
        self.assertEqual(router.resolve_session_key("qq", "", "", "qq:other"), "qq:anonymous")

    def test_build_attachment_roots_ignores_task_supplied_media_dir(self) -> None:
        roots = router.build_attachment_roots(
            raw_task={"metadata": {"phase1": {"media_dir": r"D:\outside-media"}}},
            config={"channels": {"qq": {"mediaDir": r"D:\configured-media"}}},
            settings={"attachments": {"allowedRoots": [r"D:\allowed-media"]}},
            project_root=r"D:\project",
        )
        self.assertIn(str(Path(r"D:\project").resolve(strict=False)), roots)
        self.assertIn(str(Path(r"D:\configured-media").resolve(strict=False)), roots)
        self.assertIn(str(Path(r"D:\allowed-media").resolve(strict=False)), roots)
        self.assertNotIn(str(Path(r"D:\outside-media").resolve(strict=False)), roots)

    def test_route_task_returns_local_project_history_reply(self) -> None:
        with RouterSandbox() as sandbox:
            task_dir = runtime.TASKS_DIR / "task-1"
            task_dir.mkdir(parents=True, exist_ok=True)
            runtime.write_json(
                task_dir / "status.json",
                {
                    "task_id": "task-1",
                    "project_id": "demo-app",
                    "project_root": str(sandbox.root / "workspace" / "demo-app"),
                    "phase": "finished",
                    "finished_at": "2026-04-15T11:00:00",
                    "task_name": "demo task",
                    "result": "done",
                },
            )

            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "tools": {"restrictToWorkspace": False},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": [str(Path(sandbox.temp_dir.name))]},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "history-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-history",
                    "user_request": "历史项目",
                },
            )

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "local_replied")
            self.assertIn("demo-app", result["reply_text"])
            self.assertEqual(result["reply_code"], "project_history")
            self.assertEqual(result["user_visible_status"], "completed")
            self.assertEqual(result["ack_stage"], "router")

    def test_route_task_enqueues_explicit_send_file_task_for_authorized_chat(self) -> None:
        with RouterSandbox() as sandbox:
            outside_file = Path(sandbox.temp_dir.name) / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")

            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "tools": {"restrictToWorkspace": False},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": [str(Path(sandbox.temp_dir.name))]},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "send-file-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-send-file",
                    "user_request": f"send file: {outside_file}",
                    "user_request": f"发送文件：{outside_file}",
                },
            )
            payload = runtime.read_json(task_file, default=None)
            assert isinstance(payload, dict)
            payload["user_request"] = f"send file: {outside_file}"
            runtime.write_json(task_file, payload)

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "enqueued")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "send_local_file")
            self.assertEqual(queued["system_payload"]["path"], str(outside_file.resolve(strict=False)))
            self.assertIn(str(Path(sandbox.temp_dir.name).resolve(strict=False)), queued["system_payload"]["allowed_roots"])

    def test_route_task_enqueues_health_probe_with_flush_mode(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "health-probe-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-health-probe",
                    "user_request": "health probe",
                    "metadata": {
                        "phase1": {
                            "health_probe": True,
                            "health_probe_source": "Test-Phase1Pipeline.ps1",
                            "health_probe_requested_at": "2026-04-17T10:00:00",
                        }
                    },
                },
            )

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "enqueued")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "health_probe")
            self.assertEqual(queued["routing_mode"], "flush")
            self.assertTrue(queued["metadata"]["phase1"]["health_probe"])

    def test_route_task_rejects_send_file_outside_workspace_scope_by_default(self) -> None:
        with RouterSandbox() as sandbox:
            outside_file = Path(sandbox.temp_dir.name) / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")

            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "send-file-task-default-scope.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-send-file-default-scope",
                    "user_request": f"send file: {outside_file}",
                },
            )

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "local_replied")
            self.assertEqual(result["reply_code"], "path_outside_allowed_roots")
            self.assertEqual(list(runtime.QUEUE_PENDING_DIR.glob("*.json")), [])

    def test_route_task_enqueues_freeform_history_analysis_with_context(self) -> None:
        with RouterSandbox() as sandbox:
            task_dir = runtime.TASKS_DIR / "task-1"
            task_dir.mkdir(parents=True, exist_ok=True)
            runtime.write_json(
                task_dir / "status.json",
                {
                    "task_id": "task-1",
                    "project_id": "phase1-remote-dev",
                    "project_root": str(sandbox.root / "workspace" / "phase1-remote-dev"),
                    "phase": "finished",
                    "finished_at": "2026-04-15T11:00:00",
                    "task_name": "QQ -> NanoBot -> Claude Code -> Codex",
                    "result": "remote dev workflow ready",
                },
            )

            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "phase1-remote-dev",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"historyMatchLimit": 4},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "history-analysis-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-history-analysis",
                    "user_request": "我记得我之前做过一个 QQ 到 NanoBot 再到 Claude Code 和 Codex 的项目，你找得到相关记录吗？这个项目还有继续优化空间吗？",
                },
            )

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "enqueued")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "history_analysis")
            self.assertIn("history_context", queued["metadata"]["phase1"])

    def test_route_task_enqueues_freeform_ai_file_search_for_authorized_chat(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": [str(Path(sandbox.temp_dir.name))]},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "ai-search-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-ai-file-search",
                    "user_request": "你在我电脑上面找找有没有一个文件夹保存了很多简历，有的话发一份最近一周更新的英文版给我看看",
                },
            )

            result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "enqueued")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "authorized_ai_file_search")
            self.assertTrue(queued["metadata"]["phase1"]["authorized_computer_search"])
            self.assertTrue(queued["metadata"]["phase1"]["computer_search_wants_send"])

    def test_route_task_defaults_ai_file_search_to_workspace_scope(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "tools": {"restrictToWorkspace": True},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": []},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "workspace-scope-search-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-workspace-scope-search",
                    "user_request": "浣犲湪鎴戠數鑴戜笂闈㈡壘鎵剧畝鍘嗭紝鎵惧埌灏卞彂鎴戞墜鏈?",
                },
            )

            with mock.patch.object(
                router,
                "detect_freeform_request_intent",
                return_value={
                    "kind": "ai_file_search",
                    "query": "find resume on this computer and send it back",
                    "wants_send": True,
                },
            ):
                result = router.route_task(task_file, config_path)
            self.assertEqual(result["action"], "enqueued")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            roots = queued["metadata"]["phase1"]["computer_search_roots"]
            self.assertIn(str(sandbox.root.resolve(strict=False)), roots)
            self.assertIn(str((sandbox.root / "workspace").resolve(strict=False)), roots)

    def test_route_task_uses_semantic_router_fallback_for_authorized_chat(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": [str(Path(sandbox.temp_dir.name))]},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "semantic-ai-search-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-semantic-ai-file-search",
                    "user_request": "帮我翻一下旧电脑资料，看看最近有没有更新过的英文简历，找到的话直接传我手机上",
                },
            )

            with mock.patch.object(router, "detect_freeform_request_intent", return_value=None), mock.patch.object(
                router,
                "semantic_classify_authorized_request",
                return_value={
                    "kind": "ai_file_search",
                    "query": "帮我翻一下旧电脑资料，看看最近有没有更新过的英文简历，找到的话直接传我手机上",
                    "wants_send": True,
                },
            ) as semantic_mock:
                result = router.route_task(task_file, config_path)

            self.assertEqual(result["action"], "enqueued")
            self.assertTrue(semantic_mock.called)
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "authorized_ai_file_search")
            self.assertTrue(queued["metadata"]["phase1"]["computer_search_wants_send"])

    def test_route_task_keeps_fast_path_without_semantic_router(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "computerSearch": {"allowedRoots": [str(Path(sandbox.temp_dir.name))]},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "heuristic-ai-search-task.json"
            runtime.write_json(
                task_file,
                {
                    "channel": "qq",
                    "chat_id": "chat-1",
                    "sender_id": "chat-1",
                    "message_id": "msg-fast-ai-file-search",
                    "user_request": "你在我电脑上面找找有没有一个文件夹保存了很多简历，有的话发一份更新时间是最近一周的英文版的给我看看",
                },
            )

            with mock.patch.object(router, "semantic_classify_authorized_request", side_effect=AssertionError("semantic fallback should not run")):
                result = router.route_task(task_file, config_path)

            self.assertEqual(result["action"], "enqueued")
            self.assertEqual(result["reply_code"], "queued")
            self.assertEqual(result["user_visible_status"], "queued")
            self.assertEqual(result["ack_stage"], "router")
            queue_items = list(runtime.QUEUE_PENDING_DIR.glob("*.json"))
            self.assertEqual(len(queue_items), 1)
            queued = runtime.read_json(queue_items[0], default=None)
            self.assertEqual(queued["system_action"], "authorized_ai_file_search")

    def test_route_task_duplicate_includes_standard_receipt_fields(self) -> None:
        with RouterSandbox() as sandbox:
            config_path = sandbox.root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "channels": {"qq": {"allowFrom": ["chat-1"], "mediaDir": str(sandbox.root / "media")}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(sandbox.root),
                                "allowedRoots": [str(sandbox.root)],
                            },
                            "session": {},
                            "attachments": {"allowedRoots": []},
                            "artifacts": {"maxTotalBytes": 1024 * 1024},
                            "heartbeat": {},
                            "autostart": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            task_file = sandbox.root / "duplicate-task.json"
            task_payload = {
                "channel": "qq",
                "chat_id": "chat-1",
                "sender_id": "chat-1",
                "message_id": "msg-duplicate",
                "user_request": "hello",
            }
            runtime.write_json(task_file, task_payload)
            first = router.route_task(task_file, config_path)
            self.assertEqual(first["action"], "enqueued")
            runtime.write_json(task_file, task_payload)
            second = router.route_task(task_file, config_path)
            self.assertEqual(second["action"], "duplicate")
            self.assertEqual(second["reply_code"], "duplicate")
            self.assertEqual(second["failure_category"], "duplicate")
            self.assertEqual(second["user_visible_status"], "duplicate")


if __name__ == "__main__":
    unittest.main()

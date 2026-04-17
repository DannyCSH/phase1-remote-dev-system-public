from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import phase1_runtime as runtime


class RuntimeSandbox:
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
        self.temp_dir = tempfile.TemporaryDirectory(prefix="phase1-runtime-test-")
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
        self.originals: dict[str, Path] = {}

    def __enter__(self) -> "RuntimeSandbox":
        for attr in self.PATH_ATTRS:
            self.originals[attr] = getattr(runtime, attr)
            setattr(runtime, attr, self.replacements[attr])
        runtime.ensure_runtime_layout()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for attr, original in self.originals.items():
            setattr(runtime, attr, original)
        self.temp_dir.cleanup()


class Phase1RuntimeTests(unittest.TestCase):
    def test_parse_control_command_supports_history_and_file_ops(self) -> None:
        self.assertEqual(runtime.parse_control_command("历史项目")["kind"], "project_history")
        self.assertEqual(runtime.parse_control_command("查看目录：D:\\demo")["kind"], "browse_path")
        self.assertEqual(runtime.parse_control_command("查看文件：D:\\demo\\a.txt")["kind"], "read_file")
        self.assertEqual(runtime.parse_control_command("发送文件：D:\\demo\\a.txt")["kind"], "send_file")

    def test_normalize_qq_text_strips_markdown_markup(self) -> None:
        normalized = runtime.normalize_qq_text("# 标题\n- **结果**\n`path/to/file`")
        self.assertNotIn("#", normalized)
        self.assertNotIn("**", normalized)
        self.assertNotIn("`", normalized)
        self.assertIn("- 结果", normalized)
        self.assertIn("path/to/file", normalized)

    def test_detect_freeform_request_intent_recognizes_history_and_file_search(self) -> None:
        history_intent = runtime.detect_freeform_request_intent(
            "我记得我之前用 claude code 还是 codex 做过一个项目，你找得到相关记录吗？这个项目有没有继续优化空间？"
        )
        file_intent = runtime.detect_freeform_request_intent(
            "你在我电脑上面找找有没有一个文件夹保存了很多简历，有的话发一份最近一周更新的英文版给我看看"
        )
        implicit_file_intent = runtime.detect_freeform_request_intent(
            "我的电脑上面有一张图片，应该是在E盘的下载文件夹里面，图片里面的是design指向code"
        )
        screenshot_folder_intent = runtime.detect_freeform_request_intent(
            "我电脑里面应该有一个文件夹保存着一些屏幕截图，把这些截图发送给我"
        )
        self.assertEqual(history_intent["kind"], "history_analysis")
        self.assertEqual(file_intent["kind"], "ai_file_search")
        self.assertTrue(file_intent["wants_send"])
        self.assertEqual(implicit_file_intent["kind"], "ai_file_search")
        self.assertFalse(implicit_file_intent["wants_send"])
        self.assertEqual(screenshot_folder_intent["kind"], "ai_file_search")
        self.assertTrue(screenshot_folder_intent["wants_send"])

    def test_extract_paths_from_text_is_disabled(self) -> None:
        self.assertEqual(runtime.extract_paths_from_text(r"请看 D:\secret.txt"), [])

    def test_url_only_attachment_stays_remote(self) -> None:
        normalized = runtime.normalize_attachment_item({"url": "https://example.com/demo.png"})
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["location"], "remote")
        self.assertEqual(normalized["path"], "")
        self.assertEqual(normalized["source_url"], "https://example.com/demo.png")

    def test_resolve_project_root_rejects_outside_allowed_roots(self) -> None:
        with RuntimeSandbox() as sandbox:
            settings = {
                "project": {
                    "allowedRoots": [str(sandbox.root)],
                }
            }
            with self.assertRaises(ValueError):
                runtime.resolve_project_root(
                    project_id="demo",
                    requested_root=r"C:\Windows",
                    default_project_id="demo",
                    default_project_root=str(sandbox.root),
                    settings=settings,
                )

    def test_collect_artifact_payload_filters_outside_allowed_roots(self) -> None:
        with RuntimeSandbox() as sandbox:
            task_dir = sandbox.root / "runtime" / "tasks" / "task-1"
            task_dir.mkdir(parents=True, exist_ok=True)
            allowed_dir = task_dir / "artifacts"
            allowed_dir.mkdir(parents=True, exist_ok=True)
            allowed_file = allowed_dir / "ok.txt"
            allowed_file.write_text("ok", encoding="utf-8")

            outside_file = sandbox.root / "outside.txt"
            outside_file.write_text("no", encoding="utf-8")

            runtime.write_json(
                runtime.ARTIFACTS_FILE,
                {
                    "files": [str(allowed_file), str(outside_file)],
                    "urls": [],
                    "notes": [],
                },
            )
            payload = runtime.collect_artifact_payload(
                task_dir=task_dir,
                project_root=str(sandbox.root / "project"),
                settings={"artifacts": {"maxSingleFileBytes": 1024, "allowedRoots": []}},
            )
            self.assertEqual(payload["files"], [str(allowed_file)])
            self.assertTrue(any("outside" in note or "允许目录" in note for note in payload["notes"]))

    def test_collect_artifact_payload_allows_extra_roots_for_authorized_search(self) -> None:
        with RuntimeSandbox() as sandbox:
            task_dir = sandbox.root / "runtime" / "tasks" / "task-1"
            task_dir.mkdir(parents=True, exist_ok=True)
            outside_file = sandbox.root / "outside.txt"
            outside_file.write_text("ok", encoding="utf-8")
            runtime.write_json(
                runtime.ARTIFACTS_FILE,
                {
                    "files": [str(outside_file)],
                    "urls": [],
                    "notes": [],
                },
            )
            payload = runtime.collect_artifact_payload(
                task_dir=task_dir,
                project_root=str(sandbox.root / "project"),
                settings={"artifacts": {"maxSingleFileBytes": 1024, "allowedRoots": []}},
                extra_allowed_roots=[str(sandbox.root)],
            )
            self.assertEqual(payload["files"], [str(outside_file)])

    def test_package_artifacts_for_qq_skips_files_that_exceed_upload_limit(self) -> None:
        with RuntimeSandbox() as sandbox:
            task_dir = runtime.TASKS_DIR / "task-qq"
            task_dir.mkdir(parents=True, exist_ok=True)
            artifacts_dir = task_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            safe_file = artifacts_dir / "safe.bin"
            safe_file.write_bytes(b"12345678")
            too_large_file = artifacts_dir / "large.bin"
            too_large_file.write_bytes(bytes(range(256)))

            runtime.write_json(
                runtime.ARTIFACTS_FILE,
                {
                    "files": [str(safe_file), str(too_large_file)],
                    "urls": [],
                    "notes": [],
                },
            )
            payload = runtime.package_artifacts_for_qq(
                task_dir=task_dir,
                task_id="task-qq",
                project_root=str(sandbox.root),
                settings={
                    "artifacts": {
                        "maxFiles": 12,
                        "maxTotalBytes": 1024,
                        "maxSingleFileBytes": 1024,
                        "qqMaxUploadBytes": 4200,
                        "zipThreshold": 6,
                        "allowedRoots": [],
                        "zipNamePrefix": "phase1-artifacts",
                    }
                },
                extra_allowed_roots=[str(sandbox.root)],
            )
            self.assertEqual(payload["files"], [str(safe_file)])
            self.assertTrue(any("QQ 直传限制" in note for note in payload["notes"]))

    def test_package_artifacts_for_qq_prefers_direct_send_for_many_files(self) -> None:
        with RuntimeSandbox() as sandbox:
            task_dir = runtime.TASKS_DIR / "task-many-files"
            task_dir.mkdir(parents=True, exist_ok=True)
            artifacts_dir = task_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            files: list[str] = []
            for index in range(15):
                path = artifacts_dir / f"resume-{index:02d}.txt"
                path.write_text(f"resume-{index}", encoding="utf-8")
                files.append(str(path))

            runtime.write_json(
                runtime.ARTIFACTS_FILE,
                {
                    "files": files,
                    "urls": [],
                    "notes": [],
                },
            )
            payload = runtime.package_artifacts_for_qq(
                task_dir=task_dir,
                task_id="task-many-files",
                project_root=str(sandbox.root),
                settings={
                    "artifacts": {
                        "maxFiles": 12,
                        "maxTotalBytes": 1024,
                        "maxSingleFileBytes": 1024,
                        "qqMaxUploadBytes": 5000,
                        "zipThreshold": 6,
                        "allowedRoots": [],
                        "zipNamePrefix": "phase1-artifacts",
                        "preferDirectSend": True,
                        "directSendNoticeThreshold": 8,
                    }
                },
                extra_allowed_roots=[str(sandbox.root)],
            )
            self.assertEqual(payload["files"], files)
            self.assertTrue(any("按顺序直接回传" in note for note in payload["notes"]))

    def test_package_artifacts_for_qq_can_still_zip_when_direct_send_disabled(self) -> None:
        with RuntimeSandbox() as sandbox:
            task_dir = runtime.TASKS_DIR / "task-zip"
            task_dir.mkdir(parents=True, exist_ok=True)
            artifacts_dir = task_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)

            files: list[str] = []
            for index in range(7):
                path = artifacts_dir / f"artifact-{index:02d}.txt"
                path.write_text("hello", encoding="utf-8")
                files.append(str(path))

            runtime.write_json(
                runtime.ARTIFACTS_FILE,
                {
                    "files": files,
                    "urls": [],
                    "notes": [],
                },
            )
            payload = runtime.package_artifacts_for_qq(
                task_dir=task_dir,
                task_id="task-zip",
                project_root=str(sandbox.root),
                settings={
                    "artifacts": {
                        "maxFiles": 12,
                        "maxTotalBytes": 1024 * 1024,
                        "maxSingleFileBytes": 1024 * 1024,
                        "qqMaxUploadBytes": 1024 * 1024,
                        "zipThreshold": 6,
                        "allowedRoots": [],
                        "zipNamePrefix": "phase1-artifacts",
                        "preferDirectSend": False,
                    }
                },
                extra_allowed_roots=[str(sandbox.root)],
            )
            self.assertEqual(len(payload["files"]), 1)
            self.assertTrue(payload["files"][0].endswith(".zip"))
            self.assertTrue(any("已自动打包" in note for note in payload["notes"]))

    def test_reserve_message_id_is_replay_resistant(self) -> None:
        with RuntimeSandbox():
            first = runtime.reserve_message_id("qq:test", "msg-1", ttl_seconds=3600)
            second = runtime.reserve_message_id("qq:test", "msg-1", ttl_seconds=3600)
            self.assertFalse(first)
            self.assertTrue(second)

    def test_reserve_message_id_prunes_expired_markers(self) -> None:
        with RuntimeSandbox():
            stale_marker = runtime.message_id_marker_path("qq:test", "stale-msg")
            runtime.ensure_dir(stale_marker.parent)
            stale_marker.write_text("{}", encoding="utf-8")
            stale_ts = time.time() - 7200
            os.utime(stale_marker, (stale_ts, stale_ts))

            duplicate = runtime.reserve_message_id("qq:test", "fresh-msg", ttl_seconds=60)

            self.assertFalse(duplicate)
            self.assertFalse(stale_marker.exists())
            self.assertTrue(runtime.message_id_marker_path("qq:test", "fresh-msg").exists())

    def test_read_json_falls_back_to_backup_on_corruption(self) -> None:
        with RuntimeSandbox():
            state_path = runtime.SESSION_RUNTIME_DIR / "qq_test" / "state.json"
            runtime.write_json(state_path, {"ok": True, "value": 1})
            state_path.write_text("{broken", encoding="utf-8")
            payload = runtime.read_json(state_path, default=None)
            self.assertEqual(payload, {"ok": True, "value": 1})

    def test_stop_request_ignores_backup_only_residue(self) -> None:
        with RuntimeSandbox():
            path = runtime.stop_request_path("qq:test", "session-1")
            runtime.ensure_dir(path.parent)
            runtime.write_text(
                runtime.json_backup_path(path),
                '{"session_key":"qq:test","session_id":"session-1","reason":"stale","requested_by":"test"}',
            )
            self.assertIsNone(runtime.read_stop_request("qq:test", "session-1"))

    def test_clear_stop_request_removes_primary_and_backup(self) -> None:
        with RuntimeSandbox():
            path = runtime.stop_request_path("qq:test", "session-1")
            runtime.create_stop_request("qq:test", "session-1", "cancel", "test")
            runtime.write_text(runtime.json_backup_path(path), '{"stale":true}')
            runtime.clear_stop_request("qq:test", "session-1")
            self.assertFalse(path.exists())
            self.assertFalse(runtime.json_backup_path(path).exists())

    def test_lock_with_live_pid_is_not_evicted_by_age(self) -> None:
        with RuntimeSandbox():
            lock_path = runtime.LOCKS_DIR / "demo.lock"
            runtime.write_json(
                lock_path,
                {
                    "pid": 12345,
                    "created_ts": 0,
                },
            )
            original = runtime.is_pid_alive
            try:
                runtime.is_pid_alive = lambda pid: True
                self.assertFalse(runtime._lock_is_stale(lock_path, stale_after_seconds=1))
            finally:
                runtime.is_pid_alive = original

    def test_mixed_attachment_falls_back_to_remote_when_local_path_disallowed(self) -> None:
        normalized = runtime.normalize_attachment_item(
            {
                "path": r"D:\secret.txt",
                "url": "https://example.com/demo.txt",
                "name": "demo.txt",
            },
            allowed_roots=[r"D:\allowed-root"],
        )
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["location"], "remote")
        self.assertEqual(normalized["source_url"], "https://example.com/demo.txt")

    def test_queue_depth_is_scoped_by_project_root(self) -> None:
        with RuntimeSandbox():
            task_one = {
                "task_id": "task-1",
                "session_key": "qq:test",
                "session_id": "session-a",
                "project_id": "demo",
                "project_root": r"D:\root-a",
                "received_ts": 1,
            }
            task_two = {
                "task_id": "task-2",
                "session_key": "qq:test",
                "session_id": "session-a",
                "project_id": "demo",
                "project_root": r"D:\root-b",
                "received_ts": 2,
            }
            runtime.write_json(runtime.queue_task_path(task_one), task_one)
            runtime.write_json(runtime.queue_task_path(task_two), task_two)

            self.assertEqual(runtime.queue_depth_for_session("qq:test", "session-a", "demo", r"D:\root-a"), 1)
            self.assertEqual(runtime.queue_depth_for_session("qq:test", "session-a", "demo", r"D:\root-b"), 1)

    def test_build_project_history_reply_uses_recent_task_status(self) -> None:
        with RuntimeSandbox() as sandbox:
            first_dir = runtime.TASKS_DIR / "task-1"
            first_dir.mkdir(parents=True, exist_ok=True)
            runtime.write_json(
                first_dir / "status.json",
                {
                    "task_id": "task-1",
                    "project_id": "demo-app",
                    "project_root": str(sandbox.root / "workspace" / "demo-app"),
                    "phase": "finished",
                    "finished_at": "2026-04-15T10:00:00",
                    "task_name": "first task",
                    "result": "first result",
                },
            )

            second_dir = runtime.TASKS_DIR / "task-2"
            second_dir.mkdir(parents=True, exist_ok=True)
            runtime.write_json(
                second_dir / "status.json",
                {
                    "task_id": "task-2",
                    "project_id": "demo-app",
                    "project_root": str(sandbox.root / "workspace" / "demo-app"),
                    "phase": "finished",
                    "finished_at": "2026-04-15T11:00:00",
                    "task_name": "second task",
                    "result": "latest result",
                },
            )

            reply = runtime.build_project_history_reply("demo-app")
            self.assertIn("demo-app", reply)
            self.assertIn("latest result", reply)
            self.assertIn("累计任务数：2", reply)

    def test_describe_local_path_and_preview_local_file(self) -> None:
        with RuntimeSandbox() as sandbox:
            docs_dir = sandbox.root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            note_path = docs_dir / "note.txt"
            note_path.write_text("line one\nline two\n", encoding="utf-8")

            listing = runtime.describe_local_path(str(docs_dir))
            preview = runtime.preview_local_file(str(note_path))

            self.assertIn("目录内容", listing)
            self.assertIn("note.txt", listing)
            self.assertIn("文件预览", preview)
            self.assertIn("line one", preview)

    def test_build_receipt_and_payload_with_receipt_preserve_standard_fields(self) -> None:
        receipt = runtime.build_receipt(
            stage="router",
            ack="enqueued",
            message="queued",
            task_id="task-1",
            session_key="qq:test",
            session_id="session-1",
            project_id="demo",
            project_root=r"D:\demo",
            phase="enqueued",
            reply_code="queued",
            user_visible_status="queued",
        )
        payload = runtime.payload_with_receipt({"action": "enqueued"}, receipt)
        self.assertEqual(payload["protocol"], runtime.RECEIPT_PROTOCOL)
        self.assertEqual(payload["reply_code"], "queued")
        self.assertEqual(payload["user_visible_status"], "queued")
        self.assertEqual(payload["ack_stage"], "router")

    def test_failure_category_from_code_recognizes_qq_api_errors(self) -> None:
        self.assertEqual(runtime.failure_category_from_code("qq-api-error"), "qq_api_error")
        self.assertEqual(runtime.failure_category_from_code("QQ upload API returned 850012"), "qq_api_error")

    def test_merge_task_outcome_state_tracks_last_reply_code_and_finished_at(self) -> None:
        with RuntimeSandbox() as sandbox:
            runtime.bind_running_task_state(
                session_key="qq:test",
                chat_id="chat-1",
                channel="qq",
                default_project_id="demo-app",
                default_project_root=str(sandbox.root),
                project_id="demo-app",
                project_root=str(sandbox.root),
                session_id="session-1",
                task_id="task-1",
            )
            session_state, project_state = runtime.merge_task_outcome_state(
                session_key="qq:test",
                chat_id="chat-1",
                channel="qq",
                default_project_id="demo-app",
                default_project_root=str(sandbox.root),
                project_id="demo-app",
                project_root=str(sandbox.root),
                session_id="session-1",
                task_id="task-1",
                result_text="done",
                progress="finished",
                reply_code="completed",
                failure_category="",
                finished_at="2026-04-16T12:00:00",
            )
            self.assertEqual(session_state["last_reply_code"], "completed")
            self.assertEqual(session_state["last_task_finished_at"], "2026-04-16T12:00:00")
            self.assertEqual(project_state["last_reply_code"], "completed")
            self.assertEqual(project_state["last_task_finished_at"], "2026-04-16T12:00:00")


if __name__ == "__main__":
    unittest.main()

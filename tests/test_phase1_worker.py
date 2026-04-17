from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import phase1_worker as worker

DEMO_OPENID = "DEMO_OPENID_001"


class Phase1WorkerTests(unittest.TestCase):
    def test_should_use_admin_path_requires_explicit_yes(self) -> None:
        self.assertEqual(worker.should_use_admin_path({}, "ADMIN_REQUIRED: yes"), (True, "codex_plan"))
        self.assertEqual(worker.should_use_admin_path({}, "ADMIN_REQUIRED: no"), (False, "codex_plan"))
        self.assertEqual(
            worker.should_use_admin_path(
                {"user_request": "只是分析 scheduled task，不要改系统"},
                "ADMIN_REQUIRED: maybe",
            ),
            (False, "none"),
        )

    def test_admin_authorization_matches_allow_from(self) -> None:
        task = {
            "chat_id": DEMO_OPENID,
            "sender_id": DEMO_OPENID,
            "session_key": f"qq:{DEMO_OPENID}",
        }
        channel_cfg = {"allowFrom": [DEMO_OPENID]}
        self.assertTrue(worker.is_admin_authorized_origin(task, channel_cfg))
        self.assertFalse(worker.is_admin_authorized_origin({"chat_id": "other", "session_key": "qq:other"}, channel_cfg))
        self.assertFalse(worker.is_admin_authorized_origin(task, {"allowFrom": []}))

    def test_synthetic_review_requires_trusted_flag(self) -> None:
        task_without_flag = {
            "source_task_file": str(Path(r"D:\fixture-root\runtime\tmp\test.json")),
            "session_key": "qq:test-123",
        }
        task_with_flag = {
            "source_task_file": str(Path(r"D:\fixture-root\runtime\tmp\test.json")),
            "session_key": "qq:test-123",
            "metadata": {"phase1": {"synthetic_review": True}},
        }
        self.assertFalse(worker.is_synthetic_review_artifact_task(task_without_flag))
        self.assertTrue(worker.is_synthetic_review_artifact_task(task_with_flag))

    def test_gather_batch_attachments_keeps_remote_urls(self) -> None:
        batch = [
            {
                "attachments": [
                    {
                        "path": "",
                        "source_url": "https://example.com/demo.png",
                        "location": "remote",
                    }
                ]
            }
        ]
        attachments = worker.gather_batch_attachments(batch)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["source_url"], "https://example.com/demo.png")

    def test_compose_final_message_uses_plain_text_sections(self) -> None:
        message = worker.compose_final_message(
            "# 结果\n- **完成**",
            "已验证 `pytest` 通过。",
            {"files": [], "urls": ["https://example.com"], "notes": ["- 保持观察"]},
        )
        self.assertIn("1. 结果", message)
        self.assertIn("2. 审查与验证", message)
        self.assertIn("3. 相关链接", message)
        self.assertIn("4. 补充说明", message)
        self.assertNotIn("**", message)
        self.assertNotIn("`", message)

    def test_execute_explicit_file_send_validates_path_and_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-send-file-") as temp_dir:
            file_path = Path(temp_dir) / "demo.txt"
            file_path.write_text("demo", encoding="utf-8")
            path, message = worker.execute_explicit_file_send(
                {"system_payload": {"path": str(file_path)}},
                {"agents": {"defaults": {"workspace": str(file_path.parent)}}},
                {
                    "project": {
                        "defaultId": "demo-app",
                        "defaultRoot": str(file_path.parent),
                        "allowedRoots": [str(file_path.parent)],
                    },
                    "artifacts": {"maxTotalBytes": 1024},
                },
            )
            self.assertEqual(path, file_path)
            self.assertIn("demo.txt", message)

    def test_execute_explicit_file_send_rejects_qq_oversize_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-send-file-oversize-") as temp_dir:
            file_path = Path(temp_dir) / "demo.bin"
            file_path.write_bytes(b"1234567890ABCDEF")
            with self.assertRaises(worker.Phase1Error):
                worker.execute_explicit_file_send(
                    {
                        "system_payload": {
                            "path": str(file_path),
                        }
                    },
                    {"agents": {"defaults": {"workspace": str(file_path.parent)}}},
                    {
                        "project": {
                            "defaultId": "demo-app",
                            "defaultRoot": str(file_path.parent),
                            "allowedRoots": [str(file_path.parent)],
                        },
                        "artifacts": {
                            "maxTotalBytes": 1024,
                            "qqMaxUploadBytes": 20,
                        }
                    },
                )

    def test_execute_explicit_file_send_rejects_paths_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-send-file-scope-") as temp_dir:
            root_path = Path(temp_dir)
            allowed_root = root_path / "allowed"
            allowed_root.mkdir(parents=True, exist_ok=True)
            outside_file = root_path / "outside.txt"
            outside_file.write_text("demo", encoding="utf-8")

            with self.assertRaises(worker.Phase1Error):
                worker.execute_explicit_file_send(
                    {
                        "project_id": "demo-app",
                        "project_root": str(allowed_root),
                        "system_payload": {
                            "path": str(outside_file),
                            "allowed_roots": [str(allowed_root)],
                        },
                    },
                    {
                        "tools": {"restrictToWorkspace": True},
                        "agents": {"defaults": {"workspace": str(allowed_root)}},
                    },
                    {
                        "project": {
                            "defaultId": "demo-app",
                            "defaultRoot": str(allowed_root),
                            "allowedRoots": [str(allowed_root)],
                        },
                        "artifacts": {"maxTotalBytes": 1024},
                    },
                )

    def test_execute_explicit_file_send_ignores_tampered_payload_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-send-file-tamper-") as temp_dir:
            root_path = Path(temp_dir)
            allowed_root = root_path / "allowed"
            allowed_root.mkdir(parents=True, exist_ok=True)
            outside_root = root_path / "outside"
            outside_root.mkdir(parents=True, exist_ok=True)
            outside_file = outside_root / "secret.txt"
            outside_file.write_text("secret", encoding="utf-8")

            config = {
                "tools": {"restrictToWorkspace": True},
                "agents": {"defaults": {"workspace": str(allowed_root)}},
            }
            settings = {
                "project": {
                    "defaultId": "demo-app",
                    "defaultRoot": str(allowed_root),
                    "allowedRoots": [str(allowed_root)],
                },
                "artifacts": {"maxTotalBytes": 1024},
            }
            with self.assertRaises(worker.Phase1Error):
                worker.execute_explicit_file_send(
                    {
                        "project_id": "demo-app",
                        "project_root": str(allowed_root),
                        "system_payload": {
                            "path": str(outside_file),
                            "allowed_roots": [str(outside_root)],
                        },
                    },
                    config,
                    settings,
                )

    def test_resolve_task_project_context_rejects_tampered_project_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-project-root-") as temp_dir:
            root_path = Path(temp_dir)
            allowed_root = root_path / "allowed"
            allowed_root.mkdir(parents=True, exist_ok=True)
            outside_root = root_path / "outside"
            outside_root.mkdir(parents=True, exist_ok=True)

            config = {
                "tools": {"restrictToWorkspace": True},
                "agents": {"defaults": {"workspace": str(allowed_root)}},
            }
            settings = {
                "project": {
                    "defaultId": "demo-app",
                    "defaultRoot": str(allowed_root),
                    "allowedRoots": [str(allowed_root)],
                },
                "artifacts": {"maxTotalBytes": 1024},
            }
            with self.assertRaises(worker.Phase1Error):
                worker.resolve_task_project_context(
                    {
                        "project_id": "demo-app",
                        "project_root": str(outside_root),
                    },
                    config,
                    settings,
                )

    def test_trusted_authorized_search_roots_recompute_from_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-search-roots-") as temp_dir:
            root_path = Path(temp_dir)
            allowed_root = root_path / "allowed"
            allowed_root.mkdir(parents=True, exist_ok=True)
            outside_root = root_path / "outside"
            outside_root.mkdir(parents=True, exist_ok=True)

            config = {
                "tools": {"restrictToWorkspace": True},
                "agents": {"defaults": {"workspace": str(allowed_root)}},
            }
            settings = {
                "project": {
                    "defaultId": "demo-app",
                    "defaultRoot": str(allowed_root),
                    "allowedRoots": [str(allowed_root)],
                },
                "computerSearch": {"allowedRoots": []},
                "artifacts": {"maxTotalBytes": 1024},
            }
            roots = worker.trusted_authorized_search_roots(
                {
                    "project_id": "demo-app",
                    "project_root": str(allowed_root),
                    "system_action": "authorized_ai_file_search",
                    "metadata": {
                        "phase1": {
                            "authorized_computer_search": True,
                            "computer_search_roots": [str(outside_root)],
                        }
                    },
                },
                config,
                settings,
                str(allowed_root),
                default_project_root=str(allowed_root),
            )
            self.assertIn(str(allowed_root.resolve(strict=False)), roots)
            self.assertNotIn(str(outside_root.resolve(strict=False)), roots)

    def test_is_health_probe_task_accepts_system_action_and_metadata(self) -> None:
        self.assertTrue(worker.is_health_probe_task({"system_action": "health_probe"}))
        self.assertTrue(worker.is_health_probe_task({"metadata": {"phase1": {"health_probe": True}}}))
        self.assertFalse(worker.is_health_probe_task({"system_action": "authorized_ai_file_search"}))

    def test_build_task_special_context_includes_history_and_search_roots(self) -> None:
        context = worker.build_task_special_context(
            {
                "system_action": "authorized_ai_file_search",
                "metadata": {
                    "phase1": {
                        "history_context": "历史项目摘要",
                        "authorized_computer_search": True,
                        "computer_search_roots": ["C:\\", "D:\\"],
                        "computer_search_wants_send": True,
                    }
                },
            }
        )
        self.assertIn("历史项目摘要", context)
        self.assertIn("C:\\", context)
        self.assertIn("LAST_ARTIFACTS.json", context)

    def test_should_skip_codex_review_for_authorized_computer_search(self) -> None:
        should_skip = worker.should_skip_codex_review(
            [
                {
                    "system_action": "authorized_ai_file_search",
                    "metadata": {
                        "phase1": {
                            "authorized_computer_search": True,
                            "computer_search_wants_send": True,
                        }
                    },
                }
            ],
            {"json": {"plan": {"change_scope": "project"}}},
            used_admin_path=False,
        )
        self.assertTrue(should_skip)

    def test_normalize_plan_payload_infers_text_only_scope(self) -> None:
        payload = worker.normalize_plan_payload(
            {
                "admin_required": "no",
                "goal": "只需返回一句简短中文回复，不涉及代码实现。",
                "deliverables": ["一句中文短句"],
                "steps": ["确认任务是纯文本回复", "输出单句结果"],
                "risks": [],
                "claude_primary": ["直接产出一句简短中文回复"],
                "codex_review_only": ["检查没有隐藏约束"],
            }
        )
        self.assertEqual(payload["change_scope"], "none")

    def test_find_codex_command_prefix_prefers_launcher_when_available(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-codex-launcher-") as temp_dir:
            cmd_path = Path(temp_dir) / "codex.cmd"
            ps1_path = Path(temp_dir) / "codex.ps1"
            js_path = Path(temp_dir) / "codex.js"
            cmd_path.write_text("@echo off\r\n", encoding="utf-8")
            ps1_path.write_text("", encoding="utf-8")
            js_path.write_text("", encoding="utf-8")

            original_cmd = worker.PREFERRED_CODEX_CMD
            original_ps1 = worker.PREFERRED_CODEX_PWSH
            original_js = worker.PREFERRED_CODEX_JS
            original_which = worker.shutil.which
            original_probe = worker.command_is_invocable
            try:
                worker.PREFERRED_CODEX_CMD = cmd_path
                worker.PREFERRED_CODEX_PWSH = ps1_path
                worker.PREFERRED_CODEX_JS = js_path
                worker.shutil.which = lambda name: None
                worker.command_is_invocable = lambda command: str(command[0]).endswith("codex.cmd")
                self.assertEqual(worker.find_codex_command_prefix(), [str(cmd_path)])
            finally:
                worker.PREFERRED_CODEX_CMD = original_cmd
                worker.PREFERRED_CODEX_PWSH = original_ps1
                worker.PREFERRED_CODEX_JS = original_js
                worker.shutil.which = original_which
                worker.command_is_invocable = original_probe

    def test_sanitize_codex_config_keeps_windows_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-codex-config-") as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                "[windows]\n"
                'sandbox = "elevated"\n\n'
                "[projects.'D:/phase1_remote_dev_system/runtime/tmp/review-snapshots/snapshot-123']\n"
                'trust = "trusted"\n',
                encoding="utf-8",
            )
            worker.sanitize_codex_config(config_path)
            updated = config_path.read_text(encoding="utf-8")
            self.assertIn("[windows]", updated)
            self.assertNotIn("snapshot-123", updated)

    def test_build_user_failure_message_uses_category_specific_text(self) -> None:
        stopped = worker.build_user_failure_message(worker.Phase1Error("user-stop", "manual"))
        artifact = worker.build_user_failure_message(worker.Phase1Error("local-file-delivery", "upload failed"))
        self.assertIn("任务已停止", stopped)
        self.assertIn("结果文件回传失败", artifact)

    def test_classify_delivery_warning_marks_qq_api_errors(self) -> None:
        error_type, failure_category = worker.classify_delivery_warning(
            ["files: QQ upload API returned 850012 while sending archive.zip"]
        )
        self.assertEqual(error_type, "qq-api-error")
        self.assertEqual(failure_category, "qq_api_error")

    def test_build_worker_status_payload_includes_receipt_fields(self) -> None:
        payload = worker.build_worker_status_payload(
            task_id="task-1",
            phase="finished",
            task_name="demo",
            project_id="demo-app",
            project_root=r"D:\demo",
            session_key="qq:test",
            session_id="session-1",
            started_at="2026-04-16T11:00:00",
            finished_at="2026-04-16T11:10:00",
            result="done",
            ack="finished",
            reply_code="completed",
            user_visible_status="completed",
            message="done",
        )
        self.assertEqual(payload["reply_code"], "completed")
        self.assertEqual(payload["user_visible_status"], "completed")
        self.assertEqual(payload["receipt"]["protocol"], "p1-receipt.v1")


if __name__ == "__main__":
    unittest.main()

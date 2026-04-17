from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import phase1_admin_relay as admin_relay


class Phase1AdminRelayTests(unittest.TestCase):
    def test_resolve_request_project_context_rejects_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="phase1-admin-relay-") as temp_dir:
            root_path = Path(temp_dir)
            allowed_root = root_path / "allowed"
            allowed_root.mkdir(parents=True, exist_ok=True)
            outside_root = root_path / "outside"
            outside_root.mkdir(parents=True, exist_ok=True)

            config_path = root_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {"defaults": {"workspace": str(allowed_root)}},
                        "phase1": {
                            "project": {
                                "defaultId": "demo-app",
                                "defaultRoot": str(allowed_root),
                                "allowedRoots": [str(allowed_root)],
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                admin_relay.resolve_request_project_context(
                    {
                        "project_id": "demo-app",
                        "project_root": str(outside_root),
                    },
                    config_path,
                )

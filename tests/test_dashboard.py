import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alpha_listener.dashboard import (
    build_dashboard_payload,
    is_loopback_client,
    save_dashboard_settings,
    settings_defaults,
    test_dashboard_connection,
    write_dashboard_data,
)
from alpha_listener.storage import Store


class DashboardTests(unittest.TestCase):
    def test_dashboard_payload_uses_local_sqlite_state(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = Store(workspace / "data" / "alpha.sqlite", workspace / "data")
            try:
                store.set_meta("last_scanned_block", 123)
                store.upsert_contract(
                    {
                        "address": "0x1111111111111111111111111111111111111111",
                        "tx_hash": "0xabc",
                        "discovery_source": "mint_transfer",
                        "block_number": 120,
                        "kind": "erc20",
                        "name": "Test Alpha",
                        "symbol": "TALPHA",
                        "verified": True,
                    }
                )
                store.upsert_enrichment(
                    "0x1111111111111111111111111111111111111111",
                    {
                        "queries": ["Test Alpha"],
                        "evidence": {
                            "twitter_account": "testalpha",
                            "website": "https://testalpha.example",
                            "tweet_count": 3,
                            "address_mentions": 1,
                            "project_description": "Test Alpha dashboard fixture",
                            "project_category": "token",
                            "project_positioning": "ERC20 token project",
                        },
                        "score": {
                            "score": 88,
                            "tier": "high",
                            "breakdown": {"identity_resolution": 30},
                        },
                        "status": "ok",
                    },
                )
            finally:
                store.close()

            payload = build_dashboard_payload(workspace, limit=10)
            self.assertEqual(payload["chains"]["ethereum"]["contracts"], 1)
            self.assertEqual(payload["chains"]["ethereum"]["mediumHigh"], 1)
            self.assertEqual(payload["projects"][0]["label"], "Test Alpha")
            self.assertEqual(payload["projects"][0]["twitter"], "testalpha")

            data_path = write_dashboard_data(workspace, limit=10)
            text = data_path.read_text(encoding="utf-8")
            self.assertIn("window.DASHBOARD_PROJECTS", text)
            self.assertIn("Test Alpha", text)
            json.loads(text.split("window.DASHBOARD_PROJECTS = ", 1)[1].split(";\n", 1)[0])

    def test_dashboard_payload_marks_stale_background_workers(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = workspace / "data" / "base"
            store = Store(data_dir / "alpha.sqlite", data_dir)
            try:
                old_time = "2000-01-01T00:00:00+00:00"
                store.set_meta("last_scanned_block", 100)
                store.set_meta("last_cycle_scanner_started_at", old_time)
                store.set_meta("last_cycle_scanner_progressed_at", old_time)
                store.set_meta("last_cycle_scanner_status", "running")
                store.set_meta_json(
                    "last_cycle_scanner_context_json",
                    {
                        "command": "once",
                        "max_blocks": 300,
                        "enrich_limit": 0,
                        "started_at": old_time,
                    },
                )
            finally:
                store.close()

            payload = build_dashboard_payload(workspace, limit=10)
            base = payload["chains"]["base"]
            worker = next(w for w in payload["meta"]["workers"] if w["id"] == "base-scanner")

            self.assertEqual(base["runtimeStatus"], "stale")
            self.assertEqual(worker["status"], "stale")
            self.assertTrue(worker["background"])
            self.assertGreater(payload["meta"]["workerSummary"]["stale"], 0)
            self.assertGreater(payload["meta"]["chainHealth"]["stale"], 0)

    def test_dashboard_settings_mask_reveal_and_save_env(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            workspace = Path(tmp)
            (workspace / ".env").write_text(
                "\n".join(
                    [
                        "ETHERSCAN_API_KEY=etherscan-secret-1234",
                        "OPENTWITTER_API_KEY=twitter-secret-9876",
                        "ALPHA_ETHEREUM_INTERVAL_SECONDS=600",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            masked = settings_defaults(workspace)
            self.assertEqual(masked["apiKeys"]["etherscan"], "****1234")
            self.assertEqual(masked["apiKeys"]["opentwitter"], "****9876")
            self.assertTrue(masked["apiKeyStatus"]["etherscanConfigured"])
            self.assertFalse(masked["apiKeyStatus"]["secretsRevealed"])

            revealed = settings_defaults(workspace, reveal_secrets=True)
            self.assertEqual(revealed["apiKeys"]["etherscan"], "etherscan-secret-1234")
            self.assertEqual(revealed["apiKeys"]["opentwitter"], "twitter-secret-9876")

            payload = json.loads(json.dumps(masked))
            payload["apiKeys"]["opentwitter"] = "new-twitter-token"
            payload["global"]["twitterMaxResults"] = 35
            payload["perChain"]["base"]["maxBlocksPerCycle"] = 400
            result = save_dashboard_settings(workspace, payload)

            env_text = (workspace / ".env").read_text(encoding="utf-8")
            self.assertIn("ETHERSCAN_API_KEY=etherscan-secret-1234", env_text)
            self.assertIn("OPENTWITTER_API_KEY=new-twitter-token", env_text)
            self.assertIn("ALPHA_TWITTER_MAX_RESULTS=35", env_text)
            self.assertIn("ALPHA_BASE_MAX_BLOCKS_PER_CYCLE=400", env_text)
            self.assertFalse(result["settings"]["apiKeyStatus"]["secretsRevealed"])

    def test_dashboard_settings_write_guard_is_loopback_only(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertFalse(is_loopback_client("192.0.2.10"))

    def test_dashboard_connection_test_rejects_unknown_service(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Unsupported service"):
                test_dashboard_connection(Path(tmp), "unknown")


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for open-source control-plane hardening."""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import r20_backend.notifications as notifications
import r20_backend.backup_store as backup_store
import r20_backend.net_security as net_security


class NotificationChannelRemovalTests(unittest.TestCase):
    def test_retired_personal_wechat_channel_is_not_supported(self):
        retired_channel = "wechat" + "_ilink"
        env={"R20_NOTIFY_" + retired_channel.upper() + "_ENABLED":"1"}
        self.assertNotIn(retired_channel, notifications.enabled_channels(env))
        self.assertEqual(notifications.diagnose_channel(retired_channel, env)["status"], "failed")
        ok, detail=notifications.send_channel(retired_channel, "hello", env)
        self.assertFalse(ok)
        self.assertIn("未知通知通道", detail)

    def test_dotenv_still_overrides_stale_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/".env").write_text("R20_NOTIFY_QQ_ENABLED=1\n")
            with patch.object(notifications, "ROOT", root), patch.dict(os.environ, {"R20_NOTIFY_QQ_ENABLED":"0"}, clear=True):
                self.assertEqual(notifications._env()["R20_NOTIFY_QQ_ENABLED"], "1")


class BackupTargetTests(unittest.TestCase):
    def test_local_retention_never_normalizes_to_zero(self):
        self.assertEqual(backup_store._normalize_target({"type":"local","retention":0})["retention"], 1)

    def test_job_clone_rekeys_credentials(self):
        source=backup_store._default_job(); targets=backup_store._rekey_targets(source["targets"])
        self.assertNotEqual(targets[0]["credential_ref"], source["targets"][0].get("credential_ref"))

    def test_target_config_has_only_credential_reference(self):
        target=backup_store._normalize_target({"id":"s3-main","type":"s3","endpoint":"https://s3.example.com","bucket":"bucket"})
        exported=json.dumps(target)
        self.assertIn("credential_ref", target)
        self.assertNotIn("secret_access_key", exported)


class NetworkSecurityTests(unittest.TestCase):
    def test_wechat_host_is_pinned(self):
        with patch("socket.getaddrinfo", return_value=[(2,1,6,"",("1.1.1.1",443))]):
            self.assertEqual(net_security.validate_wechat_base_url("https://ilinkai.weixin.qq.com"), "https://ilinkai.weixin.qq.com")
            with self.assertRaises(ValueError): net_security.validate_wechat_base_url("https://evil.example")

    def test_metadata_and_private_endpoints_are_blocked_by_default(self):
        with patch("socket.getaddrinfo", return_value=[(2,1,6,"",("169.254.169.254",443))]):
            with self.assertRaises(ValueError): net_security.validate_outbound_url("https://metadata.example")
        with patch("socket.getaddrinfo", return_value=[(2,1,6,"",("10.0.0.2",443))]):
            with self.assertRaises(ValueError): net_security.validate_outbound_url("https://nas.example")
            self.assertEqual(net_security.validate_outbound_url("https://nas.example", allow_private=True), "https://nas.example")

if __name__ == "__main__": unittest.main()

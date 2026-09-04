from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import r20_backend.notifications as notifications
from r20_gateway.store import GatewayStore


class ChannelBusinessCodeTests(unittest.TestCase):
    def test_wecom_http_200_error_is_failure(self):
        env={"R20_WECHAT_WEBHOOK":"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"}
        with patch.object(notifications,"validate_outbound_url",return_value=env["R20_WECHAT_WEBHOOK"]), patch.object(notifications,"_post_json",return_value=(True,"HTTP 200",{"errcode":93000,"errmsg":"denied"})):
            self.assertFalse(notifications.send_channel("wechat","x",env)[0])

    def test_telegram_http_200_error_is_failure(self):
        env={"R20_TELEGRAM_BOT_TOKEN":"T","R20_TELEGRAM_CHAT_ID":"1"}
        with patch.object(notifications,"_post_json",return_value=(True,"HTTP 200",{"ok":False,"description":"denied"})):
            self.assertFalse(notifications.send_channel("telegram","x",env)[0])

    def test_qq_http_200_error_is_failure(self):
        env={"R20_QQ_APP_ID":"A","R20_QQ_CLIENT_SECRET":"S","R20_QQ_OPENID":"O"}
        responses=[(True,"HTTP 200",{"access_token":"T"}),(True,"HTTP 200",{"code":11248,"message":"denied"})]
        with patch.object(notifications,"_post_json",side_effect=responses): self.assertFalse(notifications.send_channel("qq","x",env)[0])

    def test_diagnose_never_sends(self):
        env={"R20_TELEGRAM_BOT_TOKEN":"T","R20_TELEGRAM_CHAT_ID":"1"}
        with patch.object(notifications,"_post_json") as post:
            self.assertEqual(notifications.diagnose_channel("telegram",env)["status"],"ready"); post.assert_not_called()




class GatewayFDTests(unittest.TestCase):
    def test_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GatewayStore(Path(tmp)/"gateway.db")
            before=len(os.listdir("/proc/self/fd"))
            for i in range(150): store.set_state("x",str(i)); store.get_state("x"); store.stats()
            after=len(os.listdir("/proc/self/fd"))
            self.assertLessEqual(after-before,3)

if __name__ == "__main__": unittest.main()

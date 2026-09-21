"""Offline StopFailure/Stop tests: no models, Discord, or periodic monitoring."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import failure_hook as hook

spec = importlib.util.spec_from_file_location("progress", ROOT / "bin/progress-hook.py")
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = str(self.root / "상담역")
        Path(self.base + ".pid").write_text("123")
        (self.root / "routes.json").write_text(json.dumps({
            "bots": {"상담역": "프라이데이"}, "generalChannelId": "111", "opsLogChannelId": "222",
        }))
        self.env = patch.dict(os.environ, {"ORCH_ROOT": str(self.root),
            "ROUTES_FILE": str(self.root / "routes.json"), "ORCA_ROLE": "상담역"})
        self.env.start()
        self.api = Mock(return_value={"id": "999"})
        self.token = Mock(return_value="test-token")
        self.event = {"hook_event_name": "StopFailure", "session_id": "session-a",
            "error": "rate_limit", "last_assistant_message": "You've hit your session limit · resets 2:30am (Asia/Seoul)"}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_hook(self, event=None, seq=1, chat="111", pid="123"):
        hook.handle(self.base, chat, str(self.root), event or self.event, seq, pid, self.api, self.token)

    def state(self):
        return hook.read_json(self.base + ".failure.json")

    def test_rate_limit_notification_and_private_state(self):
        self.run_hook()
        self.assertEqual(self.api.call_count, 2)
        content = self.api.call_args.args[3]["content"]
        self.assertIn("사용량 제한", content)
        self.assertIn("2:30am (Asia/Seoul)", content)
        self.assertEqual(Path(self.base + ".failure.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.api.call_args.args[3]["allowed_mentions"], {"parse": []})

    def test_duplicate_suppressed_but_new_channel_notified(self):
        self.run_hook()
        self.run_hook(seq=2)
        self.assertEqual(self.api.call_count, 2)
        self.run_hook(seq=3, chat="333")
        self.assertEqual(self.api.call_count, 3)
        self.assertEqual(self.api.call_args.args[2], "/channels/333/messages")

    def test_changed_reset_notifies_again(self):
        self.run_hook()
        event = dict(self.event, last_assistant_message="resets 3:30am (Asia/Seoul)")
        self.run_hook(event, seq=2)
        self.assertEqual(self.api.call_count, 4)

    def test_failed_delivery_keeps_failure_and_retries_only_on_next_event(self):
        self.api.return_value = None
        self.run_hook()
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.state()["notified"], [])
        self.api.return_value = {"id": "999"}
        self.run_hook(seq=2)
        self.assertEqual(len(self.state()["notified"]), 2)

    def test_success_clears_state_and_recovery_is_not_duplicated(self):
        self.run_hook()
        self.run_hook({"hook_event_name": "Stop", "session_id": "session-a"}, seq=3)
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.api.call_count, 4)
        self.run_hook({"hook_event_name": "Stop"}, seq=4)
        self.assertEqual(self.api.call_count, 4)

    def test_delayed_failure_cannot_overwrite_success(self):
        self.run_hook({"hook_event_name": "Stop"}, seq=5)
        self.run_hook(seq=2)
        self.assertFalse(self.state()["active"])
        self.api.assert_not_called()

    def test_delayed_stop_cannot_clear_newer_failure(self):
        self.run_hook(seq=5)
        self.run_hook({"hook_event_name": "Stop"}, seq=2)
        self.assertTrue(self.state()["active"])

    def test_previous_process_cannot_change_state(self):
        self.run_hook(pid="122")
        self.assertEqual(self.state(), {})
        self.api.assert_not_called()

    def test_status_checks_pid(self):
        self.run_hook()
        with patch("builtins.print") as output:
            self.assertEqual(hook.status(self.base, "123"), 0)
            self.assertIn("응답 불가", output.call_args.args[0])
            self.assertEqual(hook.status(self.base, "999"), 1)

    def test_no_raw_error_or_secret_and_no_invented_reset(self):
        self.run_hook(dict(self.event, error="unknown-secret", error_details="password=secret",
                           last_assistant_message="Authorization: secret <@123>"))
        content = self.api.call_args.args[3]["content"]
        self.assertNotIn("secret", content)
        self.assertNotIn("<@123>", content)
        self.assertIn("제공되지 않음", content)

    def test_ops_channel_deduplicated(self):
        self.run_hook(chat="222")
        self.assertEqual(self.api.call_count, 1)

    def test_no_activity_falls_back_to_general(self):
        self.run_hook(chat="")
        self.assertEqual(self.state()["notified"], ["111", "222"])

    def test_non_terminal_event_ignored(self):
        self.run_hook({"hook_event_name": "PreToolUse"})
        self.assertEqual(self.state(), {})

    def test_missing_token_still_records_failure(self):
        self.token.return_value = ""
        self.run_hook()
        self.assertTrue(self.state()["active"])
        self.api.assert_not_called()

    def test_stopfailure_removes_progress_from_original_channel(self):
        Path(self.base + ".progress.json").write_text(json.dumps({"chat": "333", "messageId": "999"}))
        with patch.object(progress, "read_token", self.token), patch.object(progress, "api", self.api):
            progress.work(self.base, "111", str(self.root), "StopFailure", None)
        self.assertFalse(Path(self.base + ".progress.json").exists())
        self.assertEqual(self.api.call_args.args[2], "/channels/333/messages/999")

    def test_template_registers_both_completion_hooks(self):
        settings = json.loads((ROOT / "templates/progress-settings.json").read_text())
        for event in ("Stop", "StopFailure"):
            self.assertIn("progress-hook.py", settings["hooks"][event][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()

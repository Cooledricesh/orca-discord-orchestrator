import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import model_control as control


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "routes.json"
        self.cfg = {"ownerUserId": "707882541138444308", "bots": {"상담역": "프라이데이", "접수원": "해피", "workers": ["마크1"], "리뷰어": "자비스"},
            "models": {"상담역": "opus", "접수원": "sonnet", "작업자": "opus", "작업자Effort": "medium", "리뷰어": {"model": "", "effort": ""}},
            "routes": {"123": {"name": "keep", "path": "/unchanged"}}}
        self.path.write_text(json.dumps(self.cfg))
        self.env = patch.dict(os.environ, {"ORCH_ROOT": str(self.root), "ROUTES_FILE": str(self.path), "STATE_DIR_ROOT": str(self.root / "state"), "ORCA_ROLE": "상담역"})
        self.env.start()
        self.models = patch.object(control, "catalog", return_value={"gpt-6-astra": {"supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]}})
        self.models.start()
        self.event = {"authorId": self.cfg["ownerUserId"], "authorIsBot": False, "isDM": True, "channelId": "123456789012345678"}

    def tearDown(self):
        self.models.stop(); self.env.stop(); self.temp.cleanup()

    def request(self, content, **changes):
        return control.request(dict(self.event, content=content, **changes))

    def read(self):
        return json.loads(self.path.read_text())

    def state(self):
        return control.read_json(control.directory() / "state.json")

    def propose(self, command="!모델 마크1 sonnet high"):
        self.request(command)
        return self.state()["pending"]["code"]

    def confirm(self, code):
        return self.request("!모델 확인 " + code)

    def test_query_without_llm_or_routes_writes(self):
        out = self.request("!모델")
        self.assertIn("프라이데이: opus", out["text"])
        self.assertIn("CLI 기본값", out["text"])
        self.assertEqual(self.read(), self.cfg)

    def test_auth_and_dm_checks_before_any_write(self):
        for change in ({"authorId": "stranger"}, {"isDM": False}, {"authorIsBot": True}, {"channelId": "../../bad"}):
            self.assertIn("소유자", self.request("!모델 해피 opus", **change)["text"])
        self.assertFalse(control.directory().exists())

    def test_other_role_rejected(self):
        with patch.dict(os.environ, {"ORCA_ROLE": "접수원"}):
            self.assertIn("소유자", self.request("!모델")["text"])

    def test_list_from_local_catalog(self):
        self.assertIn("gpt-6-astra: high/xhigh", self.request("!모델 목록")["text"])

    def test_preview_requires_confirmation(self):
        code = self.propose()
        self.assertEqual(len(code), 8)
        self.assertEqual(self.read(), self.cfg)
        self.assertEqual((control.directory() / "state.json").stat().st_mode & 0o777, 0o600)

    def test_bad_confirmation_does_not_queue(self):
        self.propose()
        self.assertNotIn("job", self.confirm("00000000"))
        self.assertNotIn("job", self.state())

    def test_expired_and_other_chat_confirm_rejected(self):
        code = self.propose()
        self.assertNotIn("job", self.request("!모델 확인 " + code, channelId="222222222222222222"))
        with patch.object(control.time, "time", return_value=time.time() + 301):
            self.assertNotIn("job", self.confirm(code))

    def test_cancel(self):
        code = self.propose()
        self.request("!모델 취소")
        self.assertNotIn("job", self.confirm(code))

    def test_intervening_config_change_rejected(self):
        code = self.propose()
        cfg = copy.deepcopy(self.cfg); cfg["models"]["접수원"] = "haiku"
        self.path.write_text(json.dumps(cfg))
        self.assertIn("설정이 바뀌", self.confirm(code)["text"])

    def test_confirmation_replay_is_not_executed_twice(self):
        code = self.propose()
        self.assertEqual(self.confirm(code)["job"], code)
        self.assertNotIn("job", self.confirm(code))

    def test_concurrent_change_is_rejected(self):
        self.confirm(self.propose())
        self.assertIn("적용 중", self.request("!모델 해피 opus")["text"])

    def test_worker_apply_preserves_other_settings_and_never_restarts(self):
        code = self.propose(); self.confirm(code)
        with patch.object(control.subprocess, "run") as run, patch.object(control, "notify") as notify:
            control.apply_job(code); control.apply_job(code)
            run.assert_not_called(); self.assertEqual(notify.call_count, 1)
        cfg = self.read()
        self.assertEqual(cfg["models"]["작업자"], "sonnet")
        self.assertEqual(cfg["models"]["작업자Effort"], "high")
        self.assertEqual(cfg["routes"], self.cfg["routes"])
        self.assertEqual(self.state()["job"]["status"], "done")
        self.assertEqual(control.read_json(control.directory() / "routes-before.json"), self.cfg)

    def test_lead_apply_uses_fixed_script_not_shell_interpolation(self):
        code = self.propose("!모델 프라이데이 sonnet medium"); self.confirm(code)
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=0)) as run, patch.object(control, "notify"):
            control.apply_job(code)
        self.assertEqual(run.call_args.args[0], ["zsh", str(self.root / "bin/model-restart.sh"), "상담역"])
        self.assertEqual(self.read()["models"]["상담역Effort"], "medium")

    def test_failed_restart_not_reported_as_success(self):
        code = self.propose("!모델 해피 opus high"); self.confirm(code)
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=1)), patch.object(control, "notify") as notify:
            control.apply_job(code)
        self.assertEqual(self.state()["job"]["status"], "failed")
        self.assertIn("재시작을 확인하지 못", notify.call_args.args[1])

    def test_failed_restart_reports_last_stderr_line(self):
        code = self.propose("!모델 해피 opus high"); self.confirm(code)
        stderr = "noise\n[model-restart.sh] ERROR: terminal identity mismatch: worktreePath None != ORCH_ROOT\n"
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=1, stderr=stderr)), patch.object(control, "notify") as notify:
            control.apply_job(code)
        self.assertIn("원인: [model-restart.sh] ERROR: terminal identity mismatch", notify.call_args.args[1])
        self.assertNotIn("noise", self.state()["job"]["result"])
        self.assertEqual(control.restart_reason("DISCORD_BOT_TOKEN=secret"), "restart failed")

    def test_jarvis_revision_resets_next_conversation(self):
        code = self.propose("!모델 자비스 gpt-6-astra xhigh"); self.confirm(code)
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=0)), patch.object(control, "notify"):
            control.apply_job(code)
        self.assertEqual(self.read()["models"]["리뷰어"], {"model": "gpt-6-astra", "effort": "xhigh", "revision": code})

    def test_shell_injection_and_wrong_provider_rejected(self):
        for command in ("!모델 해피 $(id)", "!모델 해피 sonnet;id", "!모델 프라이데이 gpt-6-astra", "!모델 자비스 opus"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.request(command)
        self.assertEqual(self.read(), self.cfg)

    def test_invalid_effort_rejected(self):
        for command in ("!모델 해피 sonnet evil", "!모델 자비스 gpt-6-astra invalid"):
            with self.assertRaises(ValueError): self.request(command)

    def test_omitted_effort_preserves_previous(self):
        self.propose("!모델 마크 sonnet")
        self.assertEqual(self.state()["pending"]["effort"], "medium")

    def test_default_reviewer_model_and_effort(self):
        self.propose("!모델 자비스 default default")
        self.assertEqual(self.state()["pending"]["model"], "")

    def test_newlines_rejected(self):
        self.request("!모델 해피 sonnet\n!모델 확인 123")
        self.assertFalse(control.directory().exists())

    # --- 그록 작업자 ---------------------------------------------------
    def use_grok(self):
        self.cfg["bots"]["workers"] = ["마크1", {"name": "그록1", "engine": "grok"}]
        self.cfg["models"].update({"grok작업자": "grok-4.7", "grok작업자Effort": ""})
        self.path.write_text(json.dumps(self.cfg))
        grok = patch.object(control, "grok_models", return_value=["grok-4.7", "grok-4.7-build-fast", "grok-4.6"])
        grok.start(); self.addCleanup(grok.stop)

    def test_worker_names_come_from_config(self):
        self.assertEqual(self.request("!모델 마크2 sonnet")["text"], control.HELP)  # 설정에 없는 봇 → 도움말
        self.cfg["bots"]["workers"] = ["마크2"]
        self.path.write_text(json.dumps(self.cfg))
        self.propose("!모델 마크2 sonnet")
        self.assertEqual(self.state()["pending"]["role"], "작업자")

    def test_grok_worker_sets_grok_defaults_without_restart(self):
        self.use_grok()
        code = self.propose("!모델 그록1 grok-4.6 high"); self.confirm(code)
        with patch.object(control.subprocess, "run") as run, patch.object(control, "notify"):
            control.apply_job(code)
            run.assert_not_called()
        models = self.read()["models"]
        self.assertEqual((models["grok작업자"], models["grok작업자Effort"]), ("grok-4.6", "high"))
        self.assertEqual((models["작업자"], models["작업자Effort"]), ("opus", "medium"))
        self.assertIn("그록: grok-4.6", self.request("!모델")["text"])

    def test_cross_engine_models_rejected(self):
        self.use_grok()
        for command in ("!모델 그록1 opus", "!모델 그록1 grok-9", "!모델 그록1 grok-4.6 max", "!모델 마크1 grok-4.6"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.request(command)
        self.assertEqual(self.read(), self.cfg)

    def test_grok_models_parses_cli_and_reports_failure(self):
        output = "You are logged in with grok.com.\n\nDefault model: grok-4.7\n\nAvailable models:\n  * grok-4.7 (default)\n  - grok-4.6\n"
        with patch.object(control.subprocess, "run", return_value=Mock(returncode=0, stdout=output)):
            self.assertEqual(control.grok_models(), ["grok-4.7", "grok-4.6"])
        for result in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="You are not logged in.\n")):
            with patch.object(control.subprocess, "run", return_value=result), self.assertRaisesRegex(ValueError, "grok models"):
                control.grok_models()
        with patch.object(control.subprocess, "run", side_effect=FileNotFoundError), self.assertRaisesRegex(ValueError, "grok login"):
            control.grok_models()


if __name__ == "__main__":
    unittest.main()

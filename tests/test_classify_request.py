import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN))
spec = importlib.util.spec_from_file_location("classify_request", BIN / "classify-request.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)


def reply(level, confidence):
    body = json.dumps({"model": "typesafe/jev-1.13-20260917", "answers": {"difficulty": {"choice": level, "confidence": confidence}}})
    return Mock(return_value=io.BytesIO(body.encode()))


class ClassifyRequestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cr.settings({})

    def classify(self, opener):
        return lambda: cr.call_jev(self.cfg, "k", "demo", "요청", opener=opener)

    def test_mapping_by_level(self):
        expected = {"simple": ("sonnet", "medium"), "standard": ("", ""), "hard": ("fable", "high")}
        for level, (model, effort) in expected.items():
            r = cr.decide(self.cfg, classify=self.classify(reply(level, 0.9)))
            self.assertEqual((r["model"], r["effort"], r["level"], r["source"]), (model, effort, level, "jev"))

    def test_routes_mapping_and_threshold_override(self):
        cfg = cr.settings({"jevRouting": {"threshold": 0.95, "levels": {"hard": {"model": "opus", "effort": "max"}}}})
        self.assertEqual(cfg["levels"]["simple"], {"model": "sonnet", "effort": "medium"})
        r = cr.decide(cfg, classify=lambda: ("hard", 0.97))
        self.assertEqual((r["model"], r["effort"]), ("opus", "max"))
        self.assertEqual(cr.decide(cfg, classify=lambda: ("hard", 0.9))["source"], "default")

    def test_user_explicit_wins_without_calling_jev(self):
        classify = Mock(return_value=("simple", 0.99))
        r = cr.decide(self.cfg, explicit_model="fable", classify=classify)
        self.assertEqual((r["model"], r["source"]), ("fable", "user"))
        r = cr.decide(self.cfg, explicit_effort="max", classify=classify)
        self.assertEqual((r["model"], r["effort"], r["source"]), ("", "max", "user"))
        classify.assert_not_called()

    def test_grok_engine_and_disabled_skip_jev(self):
        classify = Mock(return_value=("hard", 0.99))
        self.assertEqual(cr.decide(self.cfg, engine="grok", classify=classify)["reason"], "engine")
        self.assertEqual(cr.decide(dict(self.cfg, enabled=False), classify=classify)["reason"], "disabled")
        classify.assert_not_called()

    def test_low_confidence_falls_back_to_default(self):
        r = cr.decide(self.cfg, classify=self.classify(reply("hard", 0.84)))
        self.assertEqual((r["model"], r["effort"], r["source"], r["level"], r["reason"]), ("", "", "default", "hard", "low_confidence"))

    def test_fail_open(self):
        failures = {
            "timeout": Mock(side_effect=TimeoutError()),
            "api_error": Mock(side_effect=urllib.error.URLError("refused")),
            "invalid_response": reply("impossible", 0.99),
        }
        for code, opener in failures.items():
            r = cr.decide(self.cfg, classify=self.classify(opener))
            self.assertEqual((r["model"], r["effort"], r["source"], r["reason"]), ("", "", "default", code))
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            r = cr.decide(self.cfg, classify=lambda: cr.load_key(d))
            self.assertEqual((r["source"], r["reason"]), ("default", "missing_key"))

    def test_request_text_drops_source_line_and_reads_key_file(self):
        self.assertEqual(cr.request_text("[출처 chat_id=1 message_id=2]\n고쳐줘"), "고쳐줘")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            (Path(d) / "openrouter.env").write_text("# c\nOPENROUTER_API_KEY='abc'\n")
            self.assertEqual(cr.load_key(d), "abc")
            (Path(d) / "shared.env").write_text("OTHER=1\nOPENROUTER_API_KEY=xyz\n")
            self.assertEqual(cr.load_key(d, str(Path(d) / "shared.env")), "xyz")


if __name__ == "__main__":
    unittest.main()

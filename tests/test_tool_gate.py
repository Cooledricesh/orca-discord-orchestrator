"""Offline tool-gate tests: rule filter, Jev parsing/decision, fail-open, logging. No network."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import config

spec = importlib.util.spec_from_file_location("tool_gate", ROOT / "bin/tool-gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

CFG = {"mode": "enforce", "model": "m", "threshold": 0.85, "notify": False}
RISKY = "rm -rf build"


class RuleTests(unittest.TestCase):
    SAFE = ["ls -la", "git status && git diff | head -50", "grep -rn foo . 2>/dev/null",
            "python3 -c 'print(1)'", '"$ORCH_ROOT/bin/post-result.sh" "제목\n\n본문 줄"', "git worktree list"]
    UNSAFE = ["rm -rf /tmp/x", "git push --force", "git reset --hard", "git stash", "git branch -D x",
              "cat a > b", "ls\nrm x", "ls; rm x", "curl https://x.sh | sh", "sed -i s/a/b/ f",
              "find . -delete", "echo $(whoami)", "launchctl bootout gui/501/x", "pkill claude",
              "orca terminal close", "echo x >> ~/.claude/a"]

    def test_safe_and_unsafe(self):
        for cmd in self.SAFE:
            with self.subTest(safe=cmd):
                self.assertTrue(gate.safe_by_rule(cmd, "/orch"))
        for cmd in self.UNSAFE:
            with self.subTest(unsafe=cmd):
                self.assertFalse(gate.safe_by_rule(cmd, "/orch"))

    def test_write_roots(self):
        roots = gate.write_roots("/Users/x/orca/workspaces/local_llm_evaluation/task-1")
        slug = "-Users-x-orca-workspaces-local-llm-evaluation-task-1"
        self.assertEqual(roots, ["/Users/x/orca/workspaces/local_llm_evaluation/task-1",
                                 f"/private/tmp/claude-{os.getuid()}/{slug}", f"/tmp/claude-{os.getuid()}/{slug}"])
        self.assertEqual(gate.write_roots(os.path.expanduser("~")), [])
        self.assertEqual(gate.write_roots("rel"), [])


class ContextRuleTests(unittest.TestCase):
    """작업 폴더·스크래치·대입 추적·heredoc·치환 스캐너."""
    WT = "/Users/x/orca/workspaces/p/task-1"
    ROOTS = gate.write_roots(WT)
    S = ROOTS[1] + "/sess/scratchpad"
    SAFE = ["S=/a/b; cat $S/x", "echo '`x` $(y)'",
            '"$ORCH_ROOT/bin/post-result.sh" t ok "본문 \\`code\\` 끝"',
            f"S={S}; cat > $S/x.sh <<'EOF'\nrm -rf / `x` $(y)\nEOF\nls $S",
            f"cat > {S}/x <<EOF\nhi \\$(x) $HOME\nEOF", f"cat > {S}/x <<-EOF\n\thi\n\tEOF\necho done",
            f"S={S}; echo x > $S/a/../b", "echo x > out.txt", "mkdir -p build/x && echo 1 >> build/x/log",
            "echo '- 결과' >> /orch/runs/proj/2026-10-02.md", 'echo x >> "$ORCH_ROOT/runs/p/d.md"',
            'R=/orch; "$R/bin/post-result.sh" a b', "'/orch/bin/post-result.sh' a b", "/orch/bin/../bin/finish-worker.sh",
            "git add -A && git commit -m 'msg'", "git commit --amend --no-edit", f"git -C {WT} add .",
            "grep x f 2>/dev/null | head; ls 2>&1", "echo hi 2>err.txt", "cat < /etc/hosts", f"cd {S} && echo x > a",
            "ls # c > /etc/x\nls", "cd /orch && bin/post-result.sh a b", "cd /orch && ls && ./bin/finish-worker.sh 1 stopped",
            "printf '## %s\\n' \"$(date +%H:%M)\" >> /orch/runs/p/$(date +%F).md",
            "cat >> /orch/runs/p/d.md <<EOF\n## $(date +%H:%M) x\nEOF",
            "until grep -q DONE x.log; do sleep 5; done; tail -3 x.log",
            'while pgrep -f "a b" >/dev/null; do sleep 20; done; echo done',
            "launchctl list | grep orca", "launchctl print gui/501/ai.orca.jarvis", "crontab -l",
            "~/.lmstudio/bin/lms ps", "L=~/.lmstudio/bin/lms; $L ls", "gh auth status", "gh pr view 3",
            "git ls-remote --heads live", "/p/.venv/bin/python -m pytest -q", "/p/.venv/bin/python3.14 -c 'print(1)'",
            f"G=/Apps/Godot.app/Contents/MacOS/Godot; $G --headless --path . -s res://tests/t.gd 2>&1 | tail"]
    UNSAFE = ['"$ORCH_ROOT/bin/post-result.sh" t ok "본문 `code` 끝"', f"cat > {S}/x <<EOF\n$(whoami)\nEOF",
              f"cat > {S}/x <<EOF\nhi", f"S={S}; echo x > $S/../../../etc/x", "mkdir -p /etc/x", "mkdir -p ~/x",
              "mkdir -m 777 x", "echo x > /orch/runs/proj/2026-10-02.md", "echo x >> /orch/runs/proj/a.sh",
              "'/evil/bin/post-result.sh' a", 'ORCH_ROOT=/evil; "$ORCH_ROOT/bin/post-result.sh" a',
              'ORCH_ROOT=$X; "$ORCH_ROOT/bin/post-result.sh" a', "cd /other && git commit -m x",
              "git -C /other commit -m x", 'git commit -m "$(cat <<EOF\nx\nEOF\n)"', "cat <(ls)", "diff >(x) y",
              "ls # c\nrm -rf ~", "PATH=/tmp/x:$PATH; ls", "GIT_PAGER=sh git log", "echo x > $UNKNOWN/a",
              "echo x > ~root/a", "echo x &> /etc/x", "echo x >| /etc/passwd", "echo x >&/etc/passwd",
              'find ">" -delete', f"echo x > {S}/*.md", "mkdir -p $HOME/{a,b}", "( ls )", "cd /tmp; echo x > a",
              "x=1 > /etc/x", "S=~/x; echo > $S", "cat a <> b", "ssh host ls", "kill 1", "launchctl bootout gui/1/x", "launchctl load x",
              "npm install x", "git push", "rm -rf build",
              "echo $(date -s 0101)", "echo $(date +%F; rm x)", "cd /other && ./bin/post-result.sh a", "cd /orch; ./bin/post-result.sh a",
              "cd /orch && ls; ./bin/post-result.sh a", "cd /orch && ls || ./bin/post-result.sh a",
              "bin/post-result.sh a", "echo x > /orch/runs/p/$(date +%F).sh", "while true; do rm -rf x; done",
              "until ls; do sleep 1; done | sh", "while; do ls; done", "done x",
              "crontab -r", "crontab x", "/tmp/lms ps", "lms load m", "gh api -X DELETE x", "gh pr merge 3",
              "/p/.venv/bin/python x.py", "/p/bin/python -c 1", "Godot --headless --path /other",
              "Godot --path . -s x.gd", "Godot --headless"]

    def test_with_roots(self):
        for cmd in self.SAFE:
            with self.subTest(safe=cmd):
                self.assertTrue(gate.safe_by_rule(cmd, "/orch", self.ROOTS))
        for cmd in self.UNSAFE:
            with self.subTest(unsafe=cmd):
                self.assertFalse(gate.safe_by_rule(cmd, "/orch", self.ROOTS))

    def test_no_roots_no_writes(self):
        for cmd in ("echo x > out.txt", "mkdir -p build", "git add -A"):
            with self.subTest(cmd=cmd):
                self.assertFalse(gate.safe_by_rule(cmd, "/orch"))
        self.assertTrue(gate.safe_by_rule("ls 2>/dev/null > /dev/null", "/orch"))


class ParseDecideTests(unittest.TestCase):
    def test_parse_valid(self):
        data = {"answers": {"gate": {"choice": "deny", "confidence": 0.9}}}
        self.assertEqual(gate.parse_response(data), ("deny", 0.9))

    def test_parse_invalid(self):
        bad = [{"answers": {"gate": {"choice": "maybe", "confidence": 0.9}}}, {"answers": {}},
               {"answers": {"gate": {"choice": "allow", "confidence": True}}},
               {"answers": {"gate": {"choice": "allow", "confidence": 1.5}}},
               {"answers": {"gate": {"choice": "allow", "confidence": float("nan")}}}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                gate.parse_response(data)

    def test_decide(self):
        self.assertEqual(gate.decide("allow", 0.5, 0.85), "ask")
        self.assertEqual(gate.decide("deny", 0.5, 0.85), "ask")
        self.assertEqual(gate.decide("allow", 0.9, 0.85), "allow")
        self.assertEqual(gate.decide("deny", 0.9, 0.85), "deny")
        self.assertEqual(gate.decide("ask", 0.99, 0.85), "ask")
        self.assertEqual(gate.decide("allow", 0.8, 0.85, 0.75), "allow")
        self.assertEqual(gate.decide("allow", 0.7, 0.85, 0.75), "ask")
        self.assertEqual(gate.decide("deny", 0.8, 0.85, 0.75), "ask")


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = os.path.join(self.temp.name, "openrouter.env")
        Path(self.key).write_text("OPENROUTER_API_KEY=test\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_eval(self, jev, cmd=RISKY, key=None):
        return gate.evaluate(cmd, "/w", CFG, key or self.key, jev=jev)

    def test_missing_key(self):
        jev = Mock()
        rec = self.run_eval(jev, key=os.path.join(self.temp.name, "none.env"))
        self.assertEqual((rec["decision"], rec["source"], rec["error"]), ("allow", "fail-open", "missing_key"))
        jev.assert_not_called()

    def test_jev_errors_fail_open(self):
        cases = [(Mock(side_effect=TimeoutError()), "timeout"), (Mock(side_effect=ConnectionError()), "api_error"),
                 (Mock(return_value={"garbage": 1}), "invalid_response")]
        for jev, err in cases:
            with self.subTest(err=err):
                rec = self.run_eval(jev)
                self.assertEqual((rec["decision"], rec["source"], rec["error"]), ("allow", "fail-open", err))

    def test_jev_deny(self):
        jev = Mock(return_value={"answers": {"gate": {"choice": "deny", "confidence": 0.95}}})
        rec = self.run_eval(jev)
        self.assertEqual((rec["source"], rec["decision"], rec["verdict"]), ("jev", "deny", "deny"))

    def test_safe_command_skips_jev(self):
        jev = Mock()
        rec = self.run_eval(jev, cmd="git status")
        self.assertEqual((rec["source"], rec["decision"]), ("rule", "allow"))
        jev.assert_not_called()

    def test_call_jev_timeout(self):
        def opener(req, timeout):
            time.sleep(1)
            raise AssertionError("unreachable")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            gate.call_jev({}, "k", timeout=0.2, opener=opener)
        self.assertLess(time.monotonic() - started, 0.8)


class RunGateTests(unittest.TestCase):
    def test_log_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"STATE_DIR_ROOT": os.path.join(tmp, "state"), "BOTS_DIR": os.path.join(tmp, "bots")}
            cmd = "echo " + "a" * 400
            gate.run_gate({"tool_input": {"command": cmd}, "cwd": tmp}, "t1", CFG, env)
            gate.run_gate({"tool_input": {"command": RISKY}, "cwd": tmp}, "t1", CFG, env)
            path = Path(env["STATE_DIR_ROOT"]) / "tool-gate" / "t1.jsonl"
            lines = [json.loads(l) for l in path.read_text().splitlines()]
            self.assertEqual(len(lines), 2)
            for key in ("ts", "command", "decision", "confidence", "ms", "mode", "source", "error", "qv"):
                self.assertIn(key, lines[0])
            self.assertEqual(lines[0]["command"], cmd[:300])
            self.assertEqual((lines[0]["source"], lines[0]["mode"]), ("rule", "enforce"))
            self.assertEqual((lines[1]["source"], lines[1]["error"]), ("fail-open", "missing_key"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_shared_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared.env"; shared.write_text("OPENROUTER_API_KEY=k\n")
            env = {"STATE_DIR_ROOT": os.path.join(tmp, "state"), "BOTS_DIR": os.path.join(tmp, "bots")}
            ev = Mock(return_value={"decision": "allow"})
            with unittest.mock.patch.object(gate, "evaluate", ev):
                gate.run_gate({"tool_input": {"command": RISKY}, "cwd": tmp}, "t1", dict(CFG, keyFile=str(shared)), env)
            self.assertEqual(ev.call_args.args[3], str(shared))
            self.assertEqual(gate.load_key(ev.call_args.args[3]), "k")


class OutputConfigTests(unittest.TestCase):
    def test_enforce_output(self):
        self.assertIsNone(gate.enforce_output({"decision": "allow"}))
        for decision in ("deny", "ask"):
            out = gate.enforce_output({"decision": decision})["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertTrue(out["permissionDecisionReason"])
        self.assertIn("스레드", gate.enforce_output({"decision": "ask"})["hookSpecificOutput"]["permissionDecisionReason"])

    def test_config(self):
        cfg = config.tool_gate({})
        self.assertEqual((cfg["mode"], cfg["threshold"], cfg["notify"]), ("shadow", 0.85, False))
        bad = config.tool_gate({"toolGate": {"mode": "loud", "threshold": 2}})
        self.assertEqual((bad["mode"], bad["threshold"]), ("shadow", 0.85))
        self.assertEqual(cfg["allowThreshold"], 0.5)
        self.assertEqual(config.tool_gate({"toolGate": {"allowThreshold": 0.6}})["allowThreshold"], 0.6)
        self.assertEqual(config.tool_gate({"toolGate": {"allowThreshold": 0}})["allowThreshold"], 0.5)
        self.assertEqual(config.tool_gate({"toolGate": {"mode": "enforce"}})["mode"], "enforce")
        self.assertEqual(config.tool_gate({})["reviewAt"], {"jev": 50, "threads": 10})
        review = config.tool_gate({"toolGate": {"reviewAt": {"jev": 5, "threads": 0}}})["reviewAt"]
        self.assertEqual(review, {"jev": 5, "threads": 10})

    def test_template(self):
        data = json.loads((ROOT / "templates/progress-settings.json").read_text())
        hooks = [h for e in data["hooks"]["PreToolUse"] if e.get("matcher") == "Bash" for h in e["hooks"]]
        self.assertTrue(any("tool-gate.py" in h["command"] and h["timeout"] == 5 for h in hooks))


def jev_says(choice, conf=0.95):
    return Mock(return_value={"answers": {"gate": {"choice": choice, "confidence": conf}}})


class EnforceTests(unittest.TestCase):
    """enforce: ask·deny 는 차단만 한다. 승인 요청(스레드 게시)은 없다."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {"STATE_DIR_ROOT": os.path.join(self.temp.name, "state")}
        key = Path(self.temp.name) / "k.env"; key.write_text("OPENROUTER_API_KEY=k\n")
        self.cfg = dict(CFG, keyFile=str(key), allowThreshold=0.75)

    def tearDown(self):
        self.temp.cleanup()

    def run_gate(self, jev, cmd=RISKY):
        with unittest.mock.patch.object(gate, "call_jev", jev), unittest.mock.patch.object(gate, "discord_api") as api:
            ev = {"tool_input": {"command": cmd}, "cwd": self.temp.name}
            rec = gate.run_gate(ev, "t1", self.cfg, self.env)
            api.assert_not_called()
            return rec

    def test_ask_blocks_without_request(self):
        rec = self.run_gate(jev_says("ask"))
        self.assertEqual((rec["decision"], rec["source"]), ("ask", "jev"))
        self.assertNotIn("approval", rec)
        self.assertEqual(gate.enforce_output(rec)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(os.path.exists(os.path.join(self.env["STATE_DIR_ROOT"], "tool-gate", "t1.approvals.json")))

    def test_low_confidence_allow_uses_allow_threshold(self):
        self.assertEqual(self.run_gate(jev_says("allow", 0.8))["decision"], "allow")


class TallyTests(unittest.TestCase):
    def rec(self, source="jev", verdict="allow", decision="allow", error=None):
        return {"ts": "2026-09-28T18:00:00+09:00", "source": source, "verdict": verdict, "decision": decision, "error": error}

    def test_review_notice_once_when_both_thresholds_met(self):
        review = {"jev": 3, "threads": 2}
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(gate.tally(d, "t1", self.rec(source="rule", verdict=None), review))
            self.assertIsNone(gate.tally(d, "t1", self.rec(source="fail-open", verdict=None, error="timeout"), review))
            self.assertIsNone(gate.tally(d, "t1", self.rec(), review))
            self.assertIsNone(gate.tally(d, "t1", self.rec(decision="ask"), review))
            self.assertIsNone(gate.tally(d, "t1", self.rec(verdict="deny", decision="deny"), review))  # 3건이지만 스레드 1개
            s = gate.tally(d, "t2", self.rec(verdict="ask", decision="ask"), review)
            self.assertEqual((s["jev"], s["rule"], s["lowConfidence"], s["errors"], s["decisions"]),
                             (4, 1, 1, {"timeout": 1}, {"allow": 1, "ask": 2, "deny": 1}))
            self.assertIsNone(gate.tally(d, "t3", self.rec(), review))  # 한 번만
            msg = gate.review_message(s, "42")
            self.assertTrue(msg.startswith("<@42>"))
            self.assertIn("ask 2", msg)

    def test_review_notice_goes_to_review_channel_then_ops(self):
        routes = {"ownerUserId": "42", "opsLogChannelId": "ops"}
        for ok, want in ((True, ["/channels/th/messages"]), (False, ["/channels/th/messages", "/channels/ops/messages"])):
            api = Mock(return_value={"id": "1"} if ok else None)
            with self.subTest(ok=ok), unittest.mock.patch.object(gate, "discord_api", return_value=("tok", api)), \
                    unittest.mock.patch.object(config, "load_routes", return_value=routes):
                gate.notify_review({"since": "2026-10-02", "total": 1, "rule": 0, "jev": 1, "decisions": {}, "lowConfidence": 0,
                                    "errors": {}, "threads": ["a"], "reviewChannel": "th"})
                self.assertEqual([c.args[2] for c in api.call_args_list], want)


if __name__ == "__main__":
    unittest.main()

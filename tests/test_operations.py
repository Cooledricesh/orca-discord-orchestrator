import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import operations as ops

THREAD = "1551626185551904800"
CHANNEL = "1551624954787926016"


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.area = Path(self.temp.name).resolve()
        self.root = self.area / "live root"
        self.root.mkdir()
        self.source = self.area / "task worktree"
        self.env = patch.dict(os.environ, {"ORCH_ROOT": str(self.root), "ROUTES_FILE": str(self.root / "routes.json"),
                                          "STATE_DIR_ROOT": str(self.root / "state")})
        self.env.start()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / ".gitignore").write_text("state/\nroutes.json\n")
        (self.root / "file.txt").write_text("baseline\n")
        (self.root / "bin").mkdir()
        (self.root / "bin/model-restart.sh").write_text('print -r -- "$1" >> "$ORCH_ROOT/state/restarted"\n')
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.cfg = {"bots": {"상담역": "프라이데이", "접수원": "해피", "리뷰어": "자비스", "workers": ["마크1"]},
                    "enabled": {"상담역": True, "접수원": True, "리뷰어": True, "비전": False},
                    "routes": {CHANNEL: {"name": "orchestrator", "path": str(self.root)}}}
        (self.root / "routes.json").write_text(json.dumps(self.cfg))
        self.git("worktree", "add", "-b", "task", str(self.source))
        folder = self.root / "state/threads"
        folder.mkdir(parents=True)
        (folder / (THREAD + ".json")).write_text(json.dumps({"channelId": CHANNEL, "path": str(self.source), "bot": "마크1"}))
        self.notification = patch.object(ops, "notify")
        self.notification.start()

    def tearDown(self):
        self.notification.stop()
        self.env.stop()
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.root), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def change(self, path="file.txt", text="changed\n"):
        p = self.source / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", path, cwd=self.source)
        self.git("commit", "-qm", "change", cwd=self.source)

    def execute(self, job, verify_error=None):
        job["status"] = "queued"
        ops.save(ops.job_path(job["id"]), job)
        with patch.object(ops, "verify", side_effect=verify_error) as verify:
            result = ops.execute(job["id"])
        return result, verify

    def test_plan_pins_target_and_derives_only_affected_services(self):
        self.change("sessions/상담역/CLAUDE.md", "new role")
        job = ops.plan(THREAD)
        self.assertEqual(job["services"], ["상담역"])
        self.assertEqual(job["target"], self.git("rev-parse", "HEAD", cwd=self.source))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(job["chat"], THREAD)

    def test_plan_rejects_other_project(self):
        self.change()
        self.cfg["routes"][CHANNEL]["path"] = str(self.area / "other")
        (self.root / "routes.json").write_text(json.dumps(self.cfg))
        with self.assertRaisesRegex(ValueError, "오케스트레이터"):
            ops.plan(THREAD)

    def test_dirty_live_or_candidate_never_overwritten(self):
        self.change()
        for folder in (self.root, self.source):
            with self.subTest(folder=folder):
                stray = folder / "uncommitted.txt"
                stray.write_text("keep me")
                with self.assertRaisesRegex(ValueError, "미커밋"):
                    ops.plan(THREAD)
                self.assertEqual(stray.read_text(), "keep me")
                stray.unlink()

    def test_stale_base_requires_worktree_update(self):
        self.change()
        (self.root / "other.txt").write_text("new main")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "advance")
        with self.assertRaisesRegex(ValueError, "분기"):
            ops.plan(THREAD)

    def test_changed_candidate_or_routes_invalidates_plan(self):
        self.change()
        job = ops.plan(THREAD)
        self.change(text="more")
        with self.assertRaisesRegex(ValueError, "작업 커밋"):
            ops.preflight(job)
        job = ops.plan(THREAD)
        self.cfg["new"] = True
        (self.root / "routes.json").write_text(json.dumps(self.cfg))
        with self.assertRaisesRegex(ValueError, "설정"):
            ops.preflight(job)

    def test_validation_failure_never_promotes(self):
        self.change()
        result, _ = self.execute(ops.plan(THREAD), RuntimeError("test failure"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["promoted"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_live_changes_during_validation_prevent_merge(self):
        self.change()
        def concurrent_change(_): (self.root / "new.txt").write_text("concurrent work")
        result, _ = self.execute(ops.plan(THREAD), concurrent_change)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["promoted"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_recover_merge_completed_before_state_write(self):
        self.change()
        job = ops.plan(THREAD)
        job["completed"] = ["verify"]
        self.git("update-ref", "refs/operations/" + job["id"], self.base)
        self.git("merge", "--ff-only", job["target"])
        result, verify = self.execute(job)
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["promoted"])
        verify.assert_not_called()

    def test_merge_after_tests_and_preserve_recovery_ref(self):
        self.change()
        job = ops.plan(THREAD)
        result, verify = self.execute(job)
        verify.assert_called_once_with(self.source)
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.git("rev-parse", "HEAD"), job["target"])
        self.assertEqual(self.git("rev-parse", "refs/operations/" + job["id"]), self.base)

    def test_restart_failure_retains_promoted_state_and_retry_skips_verify(self):
        self.change("sessions/상담역/CLAUDE.md", "new role")
        original_run = ops.run
        def fail_restart(argv, **kwargs):
            if argv[0] == "zsh": raise RuntimeError("restart failed")
            return original_run(argv, **kwargs)
        with patch.object(ops, "run", side_effect=fail_restart):
            result, _ = self.execute(ops.plan(THREAD))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["promoted"])
        result, verify = self.execute(result)
        self.assertEqual(result["status"], "done")
        verify.assert_not_called()
        self.assertEqual((self.root / "state/restarted").read_text().strip(), "상담역")

    def test_duplicate_apply_is_rejected(self):
        job = ops.plan(services=["접수원"])
        job["status"] = "done"
        ops.save(ops.job_path(job["id"]), job)
        with self.assertRaisesRegex(ValueError, "중복"):
            ops.start(job["id"])

    def test_detached_maintenance_runs_and_status_reports_completion(self):
        job = ops.plan(services=["접수원"])
        child = ops.start(job["id"])
        self.assertNotEqual(os.getpgid(child["pid"]), os.getpgrp())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = ops.read_job(job["id"])
            if result["status"] in ("done", "failed"): break
            time.sleep(0.05)
        self.assertEqual(result["status"], "done", (ops.job_path(job["id"]).parent / "apply.log").read_text())
        self.assertEqual((self.root / "state/restarted").read_text().strip(), "접수원")
        self.assertEqual(ops.status(job["id"])[0]["status"], "done")

    def test_interrupted_process_becomes_retryable(self):
        job = ops.plan(services=["접수원"])
        job.update(status="running", queued=time.time() - 20, pid=99999999)
        ops.save(ops.job_path(job["id"]), job)
        self.assertEqual(ops.status(job["id"])[0]["status"], "failed")

    def launchd_fixture(self):
        label = "ai.orca.sweep"
        dest = self.root / "state/launchd" / (label + ".plist")
        dest.parent.mkdir(parents=True)
        old = {"Label": label, "EnvironmentVariables": {"PATH": "/keep/original", "ORCH_ROOT": str(self.root)}, "StartInterval": 300}
        dest.write_bytes(plistlib.dumps(old))
        template = self.root / "launchd" / dest.name
        template.parent.mkdir()
        template.write_bytes(plistlib.dumps({"Label": label, "AbandonProcessGroup": True,
                                            "WorkingDirectory": "__ORCH_ROOT__", "StartInterval": 300}))
        output = f"path = {dest}\nproperties = abandon process group | inferred program\n"
        return dest, old, output

    def test_launchd_reload_preserves_environment_and_only_touches_selected_job(self):
        dest, old, output = self.launchd_fixture()
        unrelated = dest.parent / "ai.orca.jarvis.plist"
        unrelated.write_bytes(b"leave alone")
        def launch(*args): return subprocess.CompletedProcess(args, 0, output, "")
        with patch.object(ops, "launchctl", side_effect=launch) as calls:
            ops.reload_launchd("sweep", self.area / "backup")
        spec = plistlib.loads(dest.read_bytes())
        self.assertEqual(spec["EnvironmentVariables"], old["EnvironmentVariables"])
        self.assertTrue(spec["AbandonProcessGroup"])
        self.assertEqual(unrelated.read_bytes(), b"leave alone")
        self.assertTrue(all("jarvis" not in str(c) for c in calls.call_args_list))

    def test_launchd_bootstrap_failure_restores_original(self):
        dest, old, output = self.launchd_fixture()
        bootstrap_count = 0
        def launch(*args):
            nonlocal bootstrap_count
            if args[0] == "bootstrap": bootstrap_count += 1
            return subprocess.CompletedProcess(args, 1 if args[0] == "bootstrap" and bootstrap_count == 1 else 0, output, "")
        with patch.object(ops, "launchctl", side_effect=launch), self.assertRaisesRegex(RuntimeError, "복원=성공"):
            ops.reload_launchd("sweep", self.area / "backup")
        self.assertEqual(plistlib.loads(dest.read_bytes()), old)

    def test_launchd_timeout_after_bootout_restores_original(self):
        dest, old, output = self.launchd_fixture()
        bootstrap_count = 0
        def launch(*args):
            nonlocal bootstrap_count
            if args[0] == "bootstrap":
                bootstrap_count += 1
                if bootstrap_count == 1: raise subprocess.TimeoutExpired("launchctl", 30)
            return subprocess.CompletedProcess(args, 0, output, "")
        with patch.object(ops, "launchctl", side_effect=launch), self.assertRaisesRegex(RuntimeError, "복원=성공"):
            ops.reload_launchd("sweep", self.area / "backup")
        self.assertEqual(plistlib.loads(dest.read_bytes()), old)

    def test_active_sweep_not_killed(self):
        dest, _, output = self.launchd_fixture()
        before = dest.read_bytes()
        with patch.object(ops, "launchctl", return_value=subprocess.CompletedProcess([], 0, output + "pid = 123\n", "")) as calls:
            with self.assertRaisesRegex(ValueError, "실행 중"):
                ops.reload_launchd("sweep", self.area / "backup")
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(dest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

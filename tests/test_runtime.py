"""Offline regression tests: disposable Git repos, mocked Orca/Claude, no Discord."""
import fcntl
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
from urllib.parse import parse_qs, urlparse

SOURCE = Path(__file__).resolve().parent.parent
CHANNEL = "111111111111111111"
THREAD = "222222222222222222"

FAKE_ORCA = r'''#!/usr/bin/env python3
import json,os,pathlib,subprocess,sys
a=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as f:f.write(json.dumps(a)+'\n')
if a[0]=='status':
    print(json.dumps({'ok':True,'result':{'runtime':{'reachable':True}}}));sys.exit(0)
if a[:2]==['terminal','read'] and 'TEST_PROMPT_SCREEN' in os.environ:
    calls=[json.loads(x) for x in pathlib.Path(os.environ['TEST_CALLS']).read_text().splitlines()]
    sent=any(x[:2]==['terminal','send'] for x in calls)
    screen='bypass permissions on' if sent else os.environ['TEST_PROMPT_SCREEN']
    print(json.dumps({'result':{'terminal':{'status':'running','tail':[screen]}}}));sys.exit(0)
if a[:2]==['worktree','create']:
    source=a[a.index('--repo')+1].removeprefix('path:')
    mode=os.environ.get('TEST_ORCA_MODE','fail')
    if mode=='fail':sys.exit(1)
    if mode=='shared':target=source
    else:
        target=str(pathlib.Path(os.environ['TEST_AREA'])/'worker checkout')
        ref=a[a.index('--base-branch')+1]
        subprocess.run(['git','-C',source,'worktree','add','--detach',target,ref],check=True,capture_output=True)
    print(json.dumps({'result':{'worktree':{'path':target}}}));sys.exit(0)
if a[:2]==['terminal','show']:
    print(json.dumps({'result':{'terminal':json.loads(os.environ['TEST_TERMINAL_JSON'])}}));sys.exit(0)
if a[:2]==['terminal','create']:sys.exit(1)
if a[:2]==['terminal','close'] and 'TEST_CLOSE_SNAPSHOT' in os.environ:
    state=pathlib.Path(os.environ['ORCH_ROOT'])/'state'
    snapshot={'pool':json.loads((state/'pool.json').read_text()),'registry':json.loads((state/'threads'/os.environ['TEST_CLOSE_THREAD']).with_suffix('.json').read_text())}
    pathlib.Path(os.environ['TEST_CLOSE_SNAPSHOT']).write_text(json.dumps(snapshot))
sys.exit(0)
'''
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['TEST_CLAUDE_CALLS'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\n')
sys.exit(0)
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-test-")
        self.area = Path(self.temp.name).resolve()
        self.root = self.area / "install space ' $literal"
        self.root.mkdir()
        for name in ("bin", "roles", "sessions", "templates", "launchd"):
            shutil.copytree(SOURCE / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(SOURCE / "routes.example.json", self.root)
        self.project = self.area / "project space"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        self.cfg = json.loads((SOURCE / "routes.example.json").read_text())
        self.cfg.update(guildId="333333333333333333", ownerUserId="444444444444444444", generalChannelId="555555555555555555", opsLogChannelId="666666666666666666")
        self.cfg["routes"] = {CHANNEL: {"name": "project", "path": str(self.project)}}
        self.cfg["stateRoot"] = str(self.area / "channels space")
        self.save_config()
        self.env = os.environ.copy()
        for key in ("ORCH_ROOT", "ROUTES_FILE", "STATE_DIR_ROOT", "BOTS_DIR", "DRY", "ORCA_ROLE", "ORCA_THREAD_ID", "ORCA_WORKER_SPAWN_LOCK_PARENT", "ORCA_WORKER_SPAWN_LOCK_FD"):
            self.env.pop(key, None)
        self.env.update(PYTHONDONTWRITEBYTECODE="1", TEST_AREA=str(self.area), TEST_CALLS=str(self.area / "orca-calls"), TEST_CLAUDE_CALLS=str(self.area / "claude-calls"), ORCH_CODEX_AUTH_FILE=str(self.area / "auth.json"))
        for binary, content, key in (("fake-orca", FAKE_ORCA, "ORCA_BIN"), ("fake-claude", FAKE_CLAUDE, "CLAUDE_BIN")):
            p = self.area / binary
            p.write_text(content); p.chmod(0o755)
            self.env[key] = str(p)

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self):
        (self.root / "routes.json").write_text(json.dumps(self.cfg, ensure_ascii=False))

    def command(self, file, *args, **overrides):
        env = dict(self.env, **overrides)
        command = ["python3" if file.endswith(".py") else "zsh", str(self.root / "bin" / file), *args]
        return subprocess.run(command, cwd=self.area, env=env, capture_output=True, text=True, timeout=20)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def calls(self):
        f = self.area / "orca-calls"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def test_root_detected_outside_cwd_with_shell_metacharacters(self):
        r = self.command("config.py", "shell")
        self.assert_ok(r)
        value = subprocess.check_output(["zsh", "-c", r.stdout + '\nprint -r -- "$ORCH_ROOT"'], env=self.env, text=True)
        self.assertEqual(value.strip(), str(self.root))
        self.assertFalse((self.root / "state").exists())

    def test_worker_dry_run_needs_no_bot_env_and_writes_no_state(self):
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, "--title", "dry task", "--dry-run")
        self.assert_ok(r)
        self.assertIn("<new-worktree:", r.stdout)
        self.assertIn("STATE_DIR_ROOT=", r.stdout)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.area / "channels space").exists())
        self.assertEqual(self.calls(), [])

    def test_lead_dry_run_does_not_copy_env_or_install_plugin(self):
        for role in ("상담역", "접수원"):
            r = self.command("lead-up.sh", role, DRY="1")
            self.assert_ok(r)
            self.assertIn("ORCH_ROOT=", r.stdout)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.area / "channels space").exists())
        self.assertFalse((self.area / "claude-calls").exists())

    def test_dispatcher_startup_allows_only_owner_dm_and_preserves_routes(self):
        folder = Path(self.cfg["stateRoot"]) / "bots"
        folder.mkdir(parents=True)
        bot = self.cfg["bots"]["접수원"]
        (folder / f"{bot}.env").write_text("DISCORD_APP_ID=777777777777777777\nDISCORD_BOT_TOKEN=local-test-only\n")
        # Fake Orca intentionally rejects terminal creation, after access.json is prepared.
        result = self.command("lead-up.sh", "접수원")
        self.assertNotEqual(result.returncode, 0)
        access = json.loads((Path(self.cfg["stateRoot"]) / "roles" / "접수원" / "access.json").read_text())
        self.assertEqual(access["dmPolicy"], "allowlist")
        self.assertEqual(access["allowFrom"], [self.cfg["ownerUserId"]])
        self.assertEqual(set(access["groups"]), {CHANNEL, self.cfg["opsLogChannelId"]})
        self.assertNotIn("local-test-only", result.stdout + result.stderr)

    def test_model_restart_accepts_the_terminal_title_lead_up_creates(self):
        """기동 제목과 Claude가 갱신하는 봇 제목을 모두 같은 설치로 식별한다."""
        folder = Path(self.cfg["stateRoot"]) / "bots"
        folder.mkdir(parents=True)
        bot = self.cfg["bots"]["접수원"]
        (folder / f"{bot}.env").write_text("DISCORD_APP_ID=777777777777777777\nDISCORD_BOT_TOKEN=local-test-only\n")
        # Fake Orca rejects the creation but records the arguments it was given.
        self.command("lead-up.sh", "접수원")
        create = next(c for c in self.calls() if c[:2] == ["terminal", "create"])
        created_title = create[create.index("--title") + 1]

        def identity(title):
            record = json.dumps({"agentIdentity": "claude", "worktreePath": str(self.root), "title": title})
            return subprocess.run(
                ["zsh", "-c", 'source "$1"; terminal_is term_test "$(lead_title "$2")"', "test", str(self.root / "bin/lib.sh"), "접수원"],
                env=dict(self.env, TEST_TERMINAL_JSON=record), capture_output=True, text=True, timeout=10,
            ).returncode

        self.assertEqual(identity(created_title), 0)
        self.assertEqual(identity(bot), 0)
        self.assertEqual(identity("✳ " + bot), 0)
        self.assertNotEqual(identity("✳ 프라이데이"), 0)
        self.assertNotEqual(identity("접수원 (다른 설치)"), 0)

    def test_model_restart_identifies_untitled_terminal_by_claude_pid(self):
        """orphaned 터미널은 제목이 없으므로 기록된 claude PID의 --name 과 worktree로 식별한다."""
        bot = self.cfg["bots"]["상담역"]
        def sleeper(name):
            script = self.area / name
            script.write_text("import time; time.sleep(30)\n")
            proc = subprocess.Popen([sys.executable, str(script), "--name", bot])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            return str(proc.pid)

        claude, impostor = sleeper("claude"), sleeper("notclaude")

        def identity(worktree, pid, title=None, agent="claude"):
            record = json.dumps({"agentIdentity": agent, "worktreePath": worktree, "title": title, "orphaned": True})
            return subprocess.run(
                ["zsh", "-c", 'source "$1"; terminal_is term_test "$(lead_title "$2")" "$3"', "test", str(self.root / "bin/lib.sh"), "상담역", pid],
                env=dict(self.env, TEST_TERMINAL_JSON=record), capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(identity(str(self.root), claude).returncode, 0)
        rejected = identity(str(self.area), claude)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("worktreePath", rejected.stderr)
        self.assertNotEqual(identity("", claude).returncode, 0)
        self.assertNotEqual(identity(str(self.root), impostor).returncode, 0)
        self.assertNotEqual(identity(str(self.root), claude, "✳").returncode, 0)
        self.assertNotEqual(identity(str(self.root), "").returncode, 0)
        # Orca 가 agentIdentity 를 주지 않으면 제목이 맞아도 claude PID 로 확인한다.
        self.assertEqual(identity(str(self.root), claude, None, None).returncode, 0)
        self.assertEqual(identity(str(self.root), claude, "✳ " + bot, None).returncode, 0)
        self.assertNotEqual(identity(str(self.root), impostor, "✳ " + bot, None).returncode, 0)
        self.assertNotEqual(identity(str(self.root), "", "✳ " + bot, None).returncode, 0)
        self.assertNotEqual(identity(str(self.root), claude, None, "codex").returncode, 0)

    def test_dirty_orchestrator_does_not_spawn_from_stale_head(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        self.cfg["routes"][CHANNEL]["path"] = str(self.root)
        self.save_config()
        result = self.command("spawn-worker.sh", CHANNEL, THREAD, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("미커밋 변경", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_existing_lead_does_not_create_duplicate_terminal(self):
        folder = self.root / "state/leads"
        folder.mkdir(parents=True)
        (folder / "접수원.pid").write_text("123")
        (folder / "접수원.term").write_text("term_test")
        lib = self.root / "bin/lib.sh"
        lib.write_text(lib.read_text() + '\npid_is() { return 0; }\n')
        info = json.dumps({"agentIdentity": "claude", "worktreePath": str(self.root), "title": "✳ 해피"})
        result = self.command("lead-up.sh", "접수원", TEST_TERMINAL_JSON=info)
        self.assert_ok(result)
        self.assertIn("이미 실행 중", result.stdout)
        self.assertFalse(any(c[:2] == ["terminal", "create"] for c in self.calls()))

    def test_lead_start_lock_rejects_concurrent_start(self):
        lock_path = self.area / "lead.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.command("lead-start-lock.py", str(lock_path), "/not-executed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("기동이 이미 진행 중", result.stderr)

    def test_lead_model_effort_reaches_cli(self):
        self.cfg["models"]["상담역Effort"] = "high"
        self.cfg["models"]["접수원Effort"] = "medium"
        self.save_config()
        for role, effort in (("상담역", "high"), ("접수원", "medium")):
            result = self.command("lead-up.sh", role, DRY="1")
            self.assert_ok(result)
            self.assertIn(f"--effort '{effort}'", result.stdout)

    def test_startup_prompts_use_current_screen_and_selected_trust_option(self):
        for screen, expected_sends in (("❯ No, exit", 2), ("❯ Yes, I trust this folder", 1), ("bypass permissions on", 0)):
            with self.subTest(screen=screen):
                calls_file = self.area / "orca-calls"
                if calls_file.exists():
                    calls_file.unlink()
                result = subprocess.run(
                    ["zsh", "-c", 'source "$1"; sleep() { :; }; accept_prompts term_test 9', "test", str(self.root / "bin/lib.sh")],
                    env=dict(self.env, TEST_PROMPT_SCREEN=screen), capture_output=True, text=True, timeout=10,
                )
                self.assert_ok(result)
                reads = [c for c in self.calls() if c[:2] == ["terminal", "read"]]
                sends = [c for c in self.calls() if c[:2] == ["terminal", "send"]]
                self.assertTrue(reads)
                self.assertTrue(all("--screen" in c for c in reads))
                self.assertEqual(len(sends), expected_sends)
                if screen == "❯ No, exit":
                    self.assertEqual(sends[0][sends[0].index("--text") + 1], "\x1b[A")
                if sends:
                    self.assertIn("--enter", sends[-1])

    def test_explicit_path_overrides_reach_new_terminal_command(self):
        routes = self.area / "custom routes.json"
        shutil.copy(self.root / "routes.json", routes)
        state = self.area / "custom state"
        r = self.command("lead-up.sh", "접수원", DRY="1", ROUTES_FILE=str(routes), STATE_DIR_ROOT=str(state), BOTS_DIR=str(self.area / "custom bots"))
        self.assert_ok(r)
        self.assertIn(str(routes), r.stdout)
        self.assertIn(str(state / "leads"), r.stdout)
        self.assertFalse(state.exists())

    def test_disabled_roles_need_no_credentials_or_lounge(self):
        self.cfg["enabled"] = {r: False for r in self.cfg["enabled"]}
        self.save_config()
        for script, args in (("lead-up.sh", ["상담역"]), ("jarvis-up.sh", []), ("vision.sh", ["status"]), ("leads-check.sh", []), ("status.sh", [])):
            self.assert_ok(self.command(script, *args))
        self.assertFalse((self.root / "state").exists())
        self.assertEqual(self.calls(), [])

    def test_reviewer_joins_thread_only_when_enabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.cfg["enabled"]["리뷰어"] = enabled
                self.save_config()
                result = subprocess.run(
                    ["zsh", "-c", 'source "$1"; discord_api() { print -r -- "$*" >&2; }; discord_join_reviewer "$2"', "test", str(self.root / "bin/lib.sh"), THREAD],
                    env=self.env, capture_output=True, text=True, timeout=10,
                )
                self.assert_ok(result)
                self.assertEqual(result.stdout, "")
                expected = f'{self.cfg["bots"]["리뷰어"]} PUT /channels/{THREAD}/thread-members/@me\n' if enabled else ""
                self.assertEqual(result.stderr, expected)

    def test_disabled_worker_cannot_spawn(self):
        self.cfg["enabled"]["작업자"] = False
        self.save_config()
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.root / "state").exists())

    def test_pool_status_is_read_only(self):
        self.assert_ok(self.command("pool.sh", "status"))
        self.assertFalse((self.root / "state").exists())
        self.assert_ok(self.command("pool.sh", "lease", THREAD))
        pool = self.root / "state/pool.json"
        before = (pool.read_bytes(), pool.stat().st_mtime_ns)
        self.assert_ok(self.command("pool.sh", "status"))
        self.assertEqual(before, (pool.read_bytes(), pool.stat().st_mtime_ns))

    def test_failed_worktree_releases_lease_without_starting_terminal(self):
        r = self.command("spawn-worker.sh", CHANNEL, THREAD)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("worktree 생성 실패", r.stderr)
        pool = json.loads((self.root / "state/pool.json").read_text())
        self.assertIsNone(pool["마크1"]["threadId"])
        record = json.loads((self.root / f"state/threads/{THREAD}.json").read_text())
        self.assertEqual(record["status"], "failed")
        self.assertFalse(any(c[:2] == ["terminal", "create"] for c in self.calls()))
        self.assertFalse((self.area / "claude-calls").exists())

    def test_orca_returning_shared_folder_is_rejected(self):
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, TEST_ORCA_MODE="shared")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("격리 확인 실패", r.stderr)
        self.assertFalse(any(c[:2] == ["terminal", "create"] for c in self.calls()))
        self.assertIsNone(json.loads((self.root / "state/pool.json").read_text())["마크1"]["threadId"])

    def test_terminal_failure_preserves_worktree_and_releases_bot(self):
        folder = Path(self.cfg["stateRoot"]) / "bots"
        folder.mkdir(parents=True)
        (folder / "마크1.env").write_text("DISCORD_APP_ID=777777777777777777\nDISCORD_BOT_TOKEN=local-test-only\n")
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, TEST_ORCA_MODE="new")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.area / "worker checkout/.git").is_file())
        record = json.loads((self.root / f"state/threads/{THREAD}.json").read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["path"], str(self.area / "worker checkout"))
        self.assertIsNone(json.loads((self.root / "state/pool.json").read_text())["마크1"]["threadId"])
        self.assertTrue(any(c[:2] == ["terminal", "create"] for c in self.calls()))
        self.assertFalse((Path(self.cfg["stateRoot"]) / "workers" / THREAD / ".env").exists())
        self.assertTrue((folder / "마크1.env").exists())

    def test_resume_of_old_shared_session_is_refused(self):
        folder = self.root / "state/threads"
        folder.mkdir(parents=True)
        (folder / f"{THREAD}.json").write_text(json.dumps({"threadId": THREAD, "status": "done", "sessionId": "old", "path": str(self.project), "worktreeMode": "own"}))
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, "--resume", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("공유 폴더", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_sweep_does_not_reclaim_a_lease_while_spawn_is_locked(self):
        self.assert_ok(self.command("pool.sh", "lease", THREAD))
        folder = self.root / "state/threads"
        folder.mkdir(exist_ok=True)
        with (folder / f"{THREAD}.spawn.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assert_ok(self.command("sweep.sh"))
        self.assertEqual(json.loads((self.root / "state/pool.json").read_text())["마크1"]["threadId"], THREAD)
        self.assert_ok(self.command("sweep.sh"))
        self.assertIsNone(json.loads((self.root / "state/pool.json").read_text())["마크1"]["threadId"])

    def test_finish_releases_pool_and_updates_registry_before_closing_terminal(self):
        # Never access Discord in this regression test.
        lib = self.root / "bin/lib.sh"
        lib.write_text(lib.read_text() + '\ndiscord_api() { print -- "{}"; }\ndiscord_archive_thread() { :; }\nops_log() { :; }\n')
        self.assert_ok(self.command("pool.sh", "lease", THREAD))
        folder = self.root / "state/threads"
        folder.mkdir(exist_ok=True)
        (folder / f"{THREAD}.json").write_text(json.dumps({"threadId": THREAD, "status": "active", "bot": "마크1", "terminalHandle": "term_test"}))
        snapshot = self.area / "close-snapshot.json"
        result = self.command("finish-worker.sh", THREAD, "succeeded", TEST_CLOSE_THREAD=THREAD, TEST_CLOSE_SNAPSHOT=str(snapshot))
        self.assert_ok(result)
        at_close = json.loads(snapshot.read_text())
        self.assertEqual(at_close["registry"]["status"], "done")
        self.assertIsNone(at_close["pool"]["마크1"]["threadId"])

    def test_init_does_not_overwrite_existing_settings(self):
        original = (self.root / "routes.json").read_bytes()
        r = self.command("setup.py", "init")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(original, (self.root / "routes.json").read_bytes())

    def test_init_generates_four_bot_profile_with_explicit_values(self):
        (self.root / "routes.json").unlink()
        r = self.command("setup.py", "init", "--project-path", str(self.project), "--project-channel-id", CHANNEL, "--guild-id", "333333333333333333")
        self.assert_ok(r)
        data = json.loads((self.root / "routes.json").read_text())
        self.assertFalse(data["enabled"]["비전"])
        self.assertEqual(data["bots"]["workers"], ["마크1"])
        self.assertEqual(data["routes"][CHANNEL]["path"], str(self.project))
        self.assertEqual((self.root / "routes.json").stat().st_mode & 0o777, 0o600)

    def test_launchd_render_handles_spaces_and_disabled_roles(self):
        self.cfg["enabled"]["리뷰어"] = False
        self.save_config()
        output = self.area / "generated plists"
        self.assert_ok(self.command("setup.py", "launchd", "--output", str(output)))
        self.assertFalse((output / "ai.orca.jarvis.plist").exists())
        files = json.loads((output / "manifest.json").read_text())
        self.assertEqual(len(files), 3)
        for file in files:
            with open(file, "rb") as f: data = plistlib.load(f)
            self.assertEqual(data["ProgramArguments"][0], "/bin/zsh")
            self.assertTrue(Path(data["ProgramArguments"][1]).is_file())
            self.assertEqual(data["EnvironmentVariables"]["ORCH_ROOT"], str(self.root))
            self.assertNotIn("__ORCH_ROOT__", Path(file).read_text())
        self.assertEqual(self.calls(), [])

    def test_offline_doctor_reports_missing_bot_settings_without_tokens(self):
        r = self.command("setup.py", "doctor", "--offline", "--json")
        self.assertEqual(r.returncode, 1)
        result = json.loads(r.stdout)
        self.assertFalse(result["ready"])
        self.assertFalse(result["liveDiscordVerified"])
        self.assertFalse((self.root / "state").exists())

    def test_configuration_query_rejects_executable_expressions(self):
        r = self.command("config.py", "get", '.__class__')
        self.assertNotEqual(r.returncode, 0)

    def test_doctor_handles_malformed_configuration_without_traceback(self):
        for field, value in (("enabled", []), ("routes", {CHANNEL: None}), ("stateRoot", 123)):
            original = self.cfg[field]
            self.cfg[field] = value
            self.save_config()
            r = self.command("setup.py", "doctor", "--offline", "--json")
            self.assertEqual(r.returncode, 1)
            self.assertFalse(json.loads(r.stdout)["ready"])
            self.assertNotIn("Traceback", r.stderr)
            self.cfg[field] = original

    def test_invites_grant_thread_permissions_by_role_without_administrator(self):
        folder = Path(self.cfg["stateRoot"]) / "bots"
        folder.mkdir(parents=True)
        for i, name in enumerate(("프라이데이", "해피", "마크1", "자비스")):
            (folder / f"{name}.env").write_text(f"DISCORD_APP_ID={777777777777777770+i}\nDISCORD_BOT_TOKEN=never-print-this-test-token\n")
        r = self.command("setup.py", "invites")
        self.assert_ok(r)
        self.assertNotIn("never-print-this-test-token", r.stdout)
        links = {name: parse_qs(urlparse(url).query) for name, url in (line.split(": ", 1) for line in r.stdout.splitlines())}
        for name, query in links.items():
            permissions = int(query["permissions"][0])
            self.assertEqual(query["scope"], ["bot"])
            self.assertFalse(permissions & (1 << 3))
            self.assertTrue(permissions & (1 << 38))
            self.assertEqual(bool(permissions & (1 << 35)), name in ("프라이데이", "해피"))
            self.assertEqual(bool(permissions & (1 << 34)), name == "마크1")
            self.assertFalse(permissions & (1 << 4))
        self.assertEqual(self.calls(), [])

    # --- 그록 엔진 작업자 -------------------------------------------------
    def use_grok_workers(self):
        self.cfg["bots"]["workers"] = ["마크1", {"name": "그록1", "engine": "grok"}, {"name": "그록2", "engine": "grok", "model": "grok-4.7-build-fast", "effort": "high"}]
        self.save_config()

    def stable_dry_run(self, *args):
        """uuidgen 을 고정하고 임시 프롬프트 파일 이름을 지워 두 번의 dry-run 출력을 비교할 수 있게 한다."""
        fake = self.area / "fake-bin"
        fake.mkdir(exist_ok=True)
        (fake / "uuidgen").write_text("#!/bin/sh\necho 12345678-ABCD-EF00-1111-222233334444\n")
        (fake / "uuidgen").chmod(0o755)
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, "--title", "dry task", "--dry-run", *args, PATH=f"{fake}:{self.env['PATH']}")
        import re
        return r, re.sub(r"worker-prompt\.\w+", "worker-prompt.X", r.stdout)

    def test_config_workers_accepts_mixed_schema_and_keeps_string_routes(self):
        r = self.command("config.py", "workers")
        self.assert_ok(r)
        self.assertEqual(r.stdout, "마크1\tclaude\t\t\n")
        self.use_grok_workers()
        self.assertEqual(self.command("config.py", "workers").stdout, "마크1\tclaude\t\t\n그록1\tgrok\t\t\n그록2\tgrok\tgrok-4.7-build-fast\thigh\n")
        self.assertEqual(self.command("config.py", "workers", "grok").stdout.splitlines()[0], "그록1\tgrok\t\t")
        sys.path.insert(0, str(self.root / "bin"))
        try:
            import config
            self.assertIn(("작업자", "그록1"), config.enabled_bots(self.cfg))
        finally:
            sys.path.remove(str(self.root / "bin"))

    def test_doctor_rejects_unknown_engine_and_duplicate_worker(self):
        for workers, expected in (([{"name": "그록1", "engine": "gpt"}], "engine"), (["마크1", {"name": "마크1", "engine": "grok"}], "중복"), ([{"name": "", "engine": "grok"}], "빈 봇 이름")):
            with self.subTest(workers=workers):
                self.cfg["bots"]["workers"] = workers
                self.save_config()
                r = self.command("setup.py", "doctor", "--offline", "--json")
                self.assertEqual(r.returncode, 1)
                self.assertNotIn("Traceback", r.stderr)
                details = " ".join(c["detail"] for c in json.loads(r.stdout)["checks"] if not c["ok"])
                self.assertIn(expected, details)

    def test_doctor_checks_grok_prerequisites_only_with_grok_worker(self):
        names = lambda: {c["check"] for c in json.loads(self.command("setup.py", "doctor", "--offline", "--json", GROK_BIN="/nonexistent/grok").stdout)["checks"]}
        self.assertNotIn("grok", names())
        self.use_grok_workers()
        checks = names()
        self.assertIn("grok", checks)
        self.assertIn("의존성: grok-worker", checks)

    def test_pool_leases_by_engine_and_fills_per_engine(self):
        self.use_grok_workers()
        lease = lambda tid, *a: self.command("pool.sh", "lease", tid, *a)
        self.assertEqual(lease("1", "grok", "그록2").stdout.strip(), "그록2")
        self.assertEqual(lease("2", "grok").stdout.strip(), "그록1")
        self.assertEqual(lease("3", "grok").returncode, 3)  # 마크1 이 비어 있어도 grok 풀은 꽉 참
        self.assertEqual(lease("4").stdout.strip(), "마크1")
        self.assertEqual(lease("5").returncode, 3)
        self.assertEqual(lease("2", "grok").stdout.strip(), "그록1")  # 같은 스레드는 같은 봇
        status = self.command("pool.sh", "status").stdout.splitlines()
        self.assertEqual(status[0], "3/3 사용 중 (claude 1/1 · grok 2/2)")
        self.assertIn("  그록1\tgrok\t2\t", "\n".join(status))

    def test_full_grok_pool_queues_with_engine_even_if_claude_free(self):
        self.use_grok_workers()
        for tid in ("1", "2"):
            self.assert_ok(self.command("pool.sh", "lease", tid, "grok"))
        r = self.command("spawn-worker.sh", CHANNEL, THREAD, "--engine", "grok", "--title", "t")
        self.assertEqual(r.returncode, 3, r.stderr)
        record = json.loads((self.root / f"state/threads/{THREAD}.json").read_text())
        self.assertEqual((record["status"], record["engine"], record["model"], record["explicitModel"]), ("queued", "grok", "grok-4.7", False))
        self.assertIsNone(json.loads((self.root / "state/pool.json").read_text())["마크1"]["threadId"])

    def test_grok_spawn_dry_run_uses_bridge_without_claude_flags(self):
        self.use_grok_workers()
        r, out = self.stable_dry_run("--engine", "grok")
        self.assert_ok(r)
        command = out.split("--- orca terminal create ---", 1)[1]
        self.assertIn("grok-worker/bridge.ts", command)
        self.assertIn("GROK_BIN=", command)
        self.assertIn(f"threads/{THREAD}.json", command)
        for flag in ("--dangerously-skip-permissions", "--dangerously-load-development-channels", "--append-system-prompt-file", "--settings", "DISCORD_ONLY_CHATS"):
            self.assertNotIn(flag, command)
        extra = json.loads(out.split("--- registry extra ---", 1)[1].split("--- access.json", 1)[0])
        self.assertEqual(extra["engine"], "grok")
        self.assertTrue(extra["rolesFile"].endswith("roles/작업자-grok.md"))
        self.assertEqual((extra["guildId"], extra["ownerUserId"], extra["sandbox"]), (self.cfg["guildId"], self.cfg["ownerUserId"], ""))
        self.assertIn("model=grok-4.7 effort=", out)
        r, out = self.stable_dry_run("--bot", "그록2")
        self.assertIn("model=grok-4.7-build-fast effort=high", out)
        r, out = self.stable_dry_run("--bot", "그록2", "--model", "grok-4.6")
        self.assertIn("model=grok-4.6 effort=high", out)
        self.assertNotEqual(self.stable_dry_run("--bot", "그록2", "--engine", "claude")[0].returncode, 0)
        self.assertFalse((self.root / "state").exists())

    def test_default_dry_run_unchanged_by_grok_workers(self):
        r, before = self.stable_dry_run()
        self.assert_ok(r)
        self.use_grok_workers()
        r, after = self.stable_dry_run()
        self.assert_ok(r)
        self.assertEqual(before, after)
        self.assertIn("--dangerously-skip-permissions --session-id 12345678-abcd-ef00-1111-222233334444", after)
        self.assertIn("roles/작업자.md", after)
        self.assertNotIn("registry extra", after)
        self.assertNotIn("GROK_BIN", after)

    def test_finish_spawns_only_queued_request_of_released_engine(self):
        lib = self.root / "bin/lib.sh"
        lib.write_text(lib.read_text() + '\ndiscord_api() { print -- "{}"; }\ndiscord_archive_thread() { :; }\nops_log() { :; }\n')
        spawned = self.area / "retried"
        (self.root / "bin/retry-queued.sh").write_text(f'#!/bin/zsh\nprint -r -- "$1" >> {json.dumps(str(spawned))}\n')
        self.use_grok_workers()
        self.assert_ok(self.command("pool.sh", "lease", THREAD, "grok"))
        folder = self.root / "state/threads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{THREAD}.json").write_text(json.dumps({"threadId": THREAD, "status": "active", "bot": "그록1", "engine": "grok"}))
        queued = {"301": {"engine": "claude"}, "302": {"engine": "grok", "bot": "그록2"}, "303": {"engine": "grok"}}
        for i, (tid, fields) in enumerate(queued.items()):
            (folder / f"{tid}.json").write_text(json.dumps(dict(threadId=tid, status="queued", queuedAt=f"2026-01-0{i + 1}T00:00:00Z", **fields)))
        self.assert_ok(self.command("finish-worker.sh", THREAD, "succeeded", GROK_BIN="/usr/bin/false"))
        for _ in range(50):
            if spawned.exists(): break
            time.sleep(0.1)
        self.assertEqual(spawned.read_text(), "303\n")

    def test_retry_queued_passes_engine_bot_and_pinned_defaults(self):
        calls = self.area / "spawn-args"
        (self.root / "bin/spawn-worker.sh").write_text(f'#!/bin/zsh\nprint -r -- "${{(j:|:)@}}" > {json.dumps(str(calls))}\n')
        folder = self.root / "state/threads"
        folder.mkdir(parents=True)
        (folder / f"{THREAD}.json").write_text(json.dumps({"threadId": THREAD, "channelId": CHANNEL, "status": "queued", "taskTitle": "t", "currentRequest": "r",
            "requestMessageId": "", "model": "grok-4.7", "effort": "high", "explicitModel": False, "explicitEffort": True, "engine": "grok", "bot": "그록2", "newWorktree": True, "resume": False}))
        self.assert_ok(self.command("retry-queued.sh", THREAD))
        args = calls.read_text().strip().split("|")
        for pair in (["--default-model", "grok-4.7"], ["--effort", "high"], ["--engine", "grok"], ["--bot", "그록2"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)

    def test_worker_discord_react_falls_back_to_parent_channel(self):
        lib = self.root / "bin/lib.sh"
        log = self.area / "api-calls"
        lib.write_text(lib.read_text() + f'\ndiscord_api() {{ print -r -- "$1 $2 $3" >> {json.dumps(str(log))}; if [[ "$3" == /channels/{THREAD}/* ]]; then DISCORD_HTTP=404; print -- \'{{"code": 10008}}\'; return 1; fi; DISCORD_HTTP=204; }}\n')
        folder = self.root / "state/threads"
        folder.mkdir(parents=True)
        (folder / f"{THREAD}.json").write_text(json.dumps({"threadId": THREAD, "channelId": CHANNEL, "bot": "그록1"}))
        r = self.command("worker-discord.sh", "react", "999999999999999999", "✅", ORCA_THREAD_ID=THREAD)
        self.assert_ok(r)
        self.assertEqual(log.read_text().splitlines(), [
            f"그록1 PUT /channels/{THREAD}/messages/999999999999999999/reactions/%E2%9C%85/@me",
            f"그록1 PUT /channels/{CHANNEL}/messages/999999999999999999/reactions/%E2%9C%85/@me"])
        self.assertNotEqual(self.command("worker-discord.sh", "react", "1", "✅").returncode, 0)  # ORCA_THREAD_ID 없음

    def test_grok_role_shares_common_sections_with_claude_role(self):
        def section(text, title):
            return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]
        claude, grok = ((SOURCE / "roles" / name).read_text() for name in ("작업자.md", "작업자-grok.md"))
        self.assertEqual(section(claude, "작업 경로"), section(grok, "작업 경로"))
        self.assertEqual(section(claude, "인계 — 사용자가 요청했을 때만").splitlines()[0], section(grok, "인계 — 사용자가 요청했을 때만").splitlines()[0])
        self.assertIn("worker-discord.sh\" react <requestMessageId>", grok)


    def test_claude_role_sets_subagent_model_tiers(self):
        role = (SOURCE / "roles" / "작업자.md").read_text()
        rule = next(line for line in role.splitlines() if line.startswith("- 위임 모델:"))
        for model in ("sonnet", "haiku", "opus", "fable"):
            self.assertIn(f'`"{model}"`', rule)
        self.assertNotIn("SUBAGENT_MODEL", (SOURCE / "templates" / "progress-settings.json").read_text())

if __name__ == "__main__":
    unittest.main()

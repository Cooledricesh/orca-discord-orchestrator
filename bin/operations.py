#!/usr/bin/env python3
"""Local operator: pinned worktree promotion and recoverable service maintenance.

Called by owner-authorized Discord agents. This is a role-level workflow, not an
OS security boundary. No arbitrary shell command is accepted from Discord text.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime

from config import load_routes, root_path, routes_path, runtime_paths, role_enabled, worker_entries

SERVICES = ("상담역", "접수원", "리뷰어")
JOBS = ("sweep", "leads-check", "접수원-restart", "jarvis")
MCP_LOG_ROOTS = (Path.home() / "Library/Caches/claude-cli-nodejs", Path.home() / ".cache/claude-cli-nodejs")
CHANNEL_WAIT_S = 90


def directory():
    return Path(runtime_paths()["STATE_DIR_ROOT"]) / "operations"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def job_path(code):
    if not re.fullmatch(r"[a-f0-9]{12}", code):
        raise ValueError("잘못된 운영 작업 ID")
    return directory() / code / "job.json"


def read_job(code):
    return json.loads(job_path(code).read_text())


@contextmanager
def locked(name):
    directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory() / name).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("다른 운영 작업이 진행 중입니다. 완료 후 다시 요청하세요.") from None
        yield


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def clean(path):
    if git(path, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError(f"미커밋 변경이 있습니다: {path}. 변경을 검토·커밋한 뒤 다시 계획하세요.")


def fingerprint():
    return hashlib.sha256(routes_path().read_bytes()).hexdigest()


def impacts(files):
    services, jobs, notes = set(), set(), []
    for f in files:
        if f.startswith("sessions/상담역/") or f.startswith("roles/상담역-"):
            services.add("상담역")
        if f.startswith("sessions/접수원/"):
            services.add("접수원")
        if f.startswith("jarvis/") or f == "roles/자비스.md":
            services.add("리뷰어")
        if f.startswith("plugin/") or f in ("bin/config.py", "bin/lib.sh", "bin/lead-up.sh", "templates/progress-settings.json"):
            services.update(("상담역", "접수원"))
        if f == "bin/config.py":
            services.add("리뷰어")
        if f.startswith("launchd/"):
            name = Path(f).name.removeprefix("ai.orca.").removesuffix(".plist")
            if name not in JOBS:
                raise ValueError(f"자동 적용 대상이 아닌 launchd 파일: {f}")
            jobs.add(name)
    if any(f.startswith(("roles/작업자", "plugin/", "templates/")) for f in files):
        notes.append("실행 중인 마크의 대화·플러그인은 유지합니다. 새 지침/플러그인은 다음 새 작업부터 적용됩니다.")
    if any(f.startswith(("grok-worker/", "bridge-kit/")) for f in files):
        notes.append("실행 중인 그록 작업자의 브리지는 유지합니다. 변경은 다음 새 작업부터 적용됩니다.")
    # bridge-kit 은 비전(codex-worker)도 쓴다.
    if any(f.startswith(("codex-worker/", "bridge-kit/")) or f == "roles/비전.md" for f in files):
        if role_enabled(load_routes(), "비전"):
            raise ValueError("활성 비전 변경은 세션 보존 방식 확인 후 별도 적용이 필요합니다.")
        notes.append("비전은 비활성입니다. 다음 기동부터 변경을 사용합니다.")
    return [s for s in SERVICES if s in services and role_enabled(load_routes(), s)], sorted(jobs), notes


def plan(thread=None, services=(), jobs=(), chat=None):
    root = root_path()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("ORCH_ROOT가 저장소 루트가 아닙니다")
    clean(root)
    base = git(root, "rev-parse", "HEAD")
    branch = git(root, "symbolic-ref", "--quiet", "HEAD")
    source, target, files, notes = None, base, [], []
    bot = load_routes()["bots"]["상담역"]
    if thread:
        if not re.fullmatch(r"\d{16,22}", thread):
            raise ValueError("잘못된 스레드 ID")
        record = json.loads((Path(runtime_paths()["STATE_DIR_ROOT"]) / "threads" / (thread + ".json")).read_text())
        route = load_routes().get("routes", {}).get(record.get("channelId"), {})
        if Path(route.get("path", "")).resolve() != root:
            raise ValueError("오케스트레이터 프로젝트의 작업만 운영본에 적용할 수 있습니다")
        source = Path(record["path"]).resolve()
        if source == root or Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
            raise ValueError("별도 작업 worktree가 필요합니다")
        if git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise ValueError("다른 저장소의 작업입니다")
        clean(source)
        target = git(source, "rev-parse", "HEAD")
        if target == base:
            raise ValueError("운영본에 반영할 새 커밋이 없습니다")
        if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", base, target]).returncode:
            raise ValueError("운영본이 앞서거나 분기되었습니다. 작업 worktree에서 최신 운영본을 병합/리베이스하고 검증하세요.")
        files = [f for f in git(root, "diff", "--name-only", "-z", "--no-renames", base, target).split("\0") if f]
        derived_services, derived_jobs, notes = impacts(files)
        services, jobs = set(services) | set(derived_services), set(jobs) | set(derived_jobs)
        bot = record.get("bot") or bot
        chat = chat or thread
    if not set(services) <= set(SERVICES) or not set(jobs) <= set(JOBS):
        raise ValueError("지원하지 않는 운영 대상")
    for service in services:
        if not role_enabled(load_routes(), service):
            raise ValueError(f"비활성 역할: {service}")
    if not source and not services and not jobs:
        raise ValueError("작업 스레드 또는 재시작/launchd 대상을 지정하세요")
    if chat and not re.fullmatch(r"\d{16,22}", chat):
        raise ValueError("잘못된 결과 보고 채널")
    code = secrets.token_hex(6)
    data = dict(id=code, status="planned", created=time.time(), root=str(root), source=str(source) if source else None,
                base=base, target=target, branch=branch, config=fingerprint(), files=files,
                services=[s for s in SERVICES if s in services], launchd=sorted(jobs), notes=notes,
                chat=chat, bot=bot, completed=[], promoted=False)
    save(job_path(code), data)
    return data


def preflight(job):
    root = root_path()
    if str(root) != job["root"] or fingerprint() != job["config"]:
        raise ValueError("운영 경로/설정이 바뀌었습니다. 새 계획이 필요합니다")
    if git(root, "symbolic-ref", "--quiet", "HEAD") != job["branch"]:
        raise ValueError("운영 브랜치가 바뀌었습니다")
    clean(root)
    head = git(root, "rev-parse", "HEAD")
    # Recover a crash between a completed merge and the state-file write.
    if job["source"] and not job["promoted"] and head == job["target"]:
        try:
            previous = git(root, "rev-parse", "refs/operations/" + job["id"])
        except subprocess.CalledProcessError:
            previous = None
        if previous == job["base"] and "verify" in job["completed"]:
            job["promoted"] = True
            save(job_path(job["id"]), job)
    expected = job["target"] if job["promoted"] else job["base"]
    if head != expected:
        raise ValueError("운영 커밋이 바뀌었습니다. 새 계획이 필요합니다")
    if job["source"]:
        source = Path(job["source"])
        clean(source)
        if git(source, "rev-parse", "HEAD") != job["target"]:
            raise ValueError("작업 커밋이 바뀌었습니다. 새 계획이 필요합니다")


def run(argv, cwd=None, timeout=300):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, stdin=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"명령 실패(exit {result.returncode}): {Path(str(argv[0])).name}")


def verify(source):
    # Use the trusted operator's verification entry point, running in the candidate.
    run(["python3", "-m", "unittest", "discover", "-s", "tests"], cwd=source, timeout=600)
    for folder in ("codex-worker", "bridge-kit", "grok-worker", "jarvis", "plugin/discord-orca"):
        cwd = Path(source) / folder
        if folder == "bridge-kit" and not (cwd / "package.json").exists():
            continue  # 패키지가 아니면 codex-worker·grok-worker 의 typecheck/test 가 검증한다
        run(["bun", "install", "--ignore-scripts"], cwd=cwd)
        if folder != "plugin/discord-orca":
            run(["bun", "run", "typecheck"], cwd=cwd)
        run(["bun", "test"], cwd=cwd, timeout=600)
    for path in sorted((Path(source) / "bin").glob("*.sh")) + [Path(source) / "lead.sh"]:
        run(["zsh", "-n", path])


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def reload_launchd(name, backup):
    label = "ai.orca." + name
    domain = f"gui/{os.getuid()}"
    current = launchctl("print", domain + "/" + label)
    if current.returncode:
        raise ValueError(f"{label}이 등록되어 있지 않습니다. 최초 등록은 setup.py launchd 절차를 사용하세요.")
    match = re.search(r"^\s*path = (.+)$", current.stdout, re.M)
    if not match:
        raise ValueError("등록된 launchd 파일 경로를 확인할 수 없습니다")
    destination = Path(match.group(1)).resolve()
    expected = Path(runtime_paths()["STATE_DIR_ROOT"]) / "launchd" / (label + ".plist")
    if destination != expected.resolve():
        raise ValueError("다른 설치 경로의 launchd job입니다. 자동으로 덮어쓰지 않습니다")
    if name != "jarvis" and re.search(r"^\s*pid = \d+", current.stdout, re.M):
        raise ValueError(f"{label}이 실행 중입니다. 종료 후 같은 운영 작업을 retry 하세요")
    original = destination.read_bytes()
    old = plistlib.loads(original)
    paths = runtime_paths()
    def expand(value):
        if isinstance(value, dict): return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list): return [expand(v) for v in value]
        if isinstance(value, str):
            for key, val in dict(paths, HOME=str(Path.home())).items():
                value = value.replace("__" + key + "__", val)
        return value
    spec = expand(plistlib.loads((root_path() / "launchd" / (label + ".plist")).read_bytes()))
    if spec.get("Label") != label:
        raise ValueError("launchd Label 불일치")
    # Keep the installed execution environment, not this agent's PATH/CODEX_HOME.
    spec["EnvironmentVariables"] = old.get("EnvironmentVariables", {})
    backup.mkdir(parents=True, exist_ok=True)
    (backup / (label + ".plist")).write_bytes(original)
    temp = destination.with_suffix(".ops.tmp")
    temp.write_bytes(plistlib.dumps(spec))
    run(["plutil", "-lint", temp])
    if launchctl("bootout", domain + "/" + label).returncode:
        temp.unlink()
        raise RuntimeError(f"{label} 해제 실패; 파일은 보존했습니다")
    try:
        temp.replace(destination)
        loaded = launchctl("bootstrap", domain, str(destination))
        check = launchctl("print", domain + "/" + label)
        valid = not loaded.returncode and not check.returncode
        if spec.get("AbandonProcessGroup"):
            valid = valid and "abandon process group" in check.stdout
        if not valid:
            raise RuntimeError("launchd 검증 실패")
    except Exception:
        restored = False
        try:
            launchctl("bootout", domain + "/" + label)
            temp.write_bytes(original)
            temp.replace(destination)
            restored = (launchctl("bootstrap", domain, str(destination)).returncode == 0
                        and launchctl("print", domain + "/" + label).returncode == 0)
        except Exception:
            pass
        raise RuntimeError(f"{label} 재등록 검증 실패; 이전 설정 복원={'성공' if restored else '실패'}") from None


def channel_event(cwd, since):
    """since 이후 cwd 세션의 discord-orca 채널 등록 로그: (registered|skipped, epoch) 또는 None."""
    want = unicodedata.normalize("NFC", str(cwd))
    found = None
    roots = [Path(os.environ["ORCH_MCP_LOG_ROOT"])] if os.environ.get("ORCH_MCP_LOG_ROOT") else MCP_LOG_ROOTS
    for base in roots:
        for path in base.glob("*/mcp-logs-plugin-discord-orca-discord/*.jsonl"):
            try:
                if path.stat().st_mtime < since:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if "Channel notifications" not in line:
                    continue
                try:
                    entry = json.loads(line)
                    at = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if at < since or unicodedata.normalize("NFC", str(entry.get("cwd", ""))) != want:
                    continue
                kind = "registered" if "registered" in entry.get("debug", "") else "skipped"
                if found is None or at > found[1]:
                    found = (kind, at)
    return found


def check_channel(role, since):
    """재시작한 상담역·접수원이 Discord 메시지를 실제로 받도록 채널 등록됐는지 확인한다."""
    deadline = time.time() + CHANNEL_WAIT_S
    while True:
        event = channel_event(root_path() / "sessions" / role, since)
        if event and event[0] == "registered":
            return f"{role}: 채널 수신 등록 확인 ({time.strftime('%H:%M:%S', time.localtime(event[1]))})"
        if event:
            raise RuntimeError(f"{role} 채널 수신 등록 거부(skipped) — 메시지를 받지 못합니다")
        if time.time() >= deadline:
            raise RuntimeError(f"{role} 채널 수신 등록 로그가 {CHANNEL_WAIT_S}초 안에 없습니다")
        time.sleep(2)


def notify(job):
    if not job.get("chat"):
        return
    cfg = load_routes()
    allowed = [cfg.get("bots", {}).get("상담역"), *(e["name"] for e in worker_entries(cfg))]
    if job["bot"] not in allowed:
        raise ValueError("결과 보고 봇이 현재 설정에 없습니다")
    path = Path(runtime_paths()["BOTS_DIR"]) / (job["bot"] + ".env")
    token = next(line.split("=", 1)[1].strip().strip('\"\'') for line in path.read_text().splitlines() if line.startswith("DISCORD_BOT_TOKEN="))
    text = (f"운영 작업 {job['id']}: {job['status']}\n"
            f"운영 커밋: {git(root_path(), 'rev-parse', '--short', 'HEAD')}\n"
            f"{job.get('result', '')}\n" + "\n".join(job["notes"]))[:1900]
    request = urllib.request.Request(f"https://discord.com/api/v10/channels/{job['chat']}/messages",
        data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Authorization": "Bot " + token, "Content-Type": "application/json", "User-Agent": "orchestrator-operations/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def execute(code):
    with locked("apply.lock"):
        job = read_job(code)
        if job["status"] != "queued":
            return job
        job.update(status="running", pid=os.getpid())
        save(job_path(code), job)
        def step(name, action):
            if name in job["completed"]:
                return
            job["phase"] = name
            save(job_path(code), job)
            action()
            job["completed"].append(name)
            save(job_path(code), job)
        try:
            job["phase"] = "preflight"
            preflight(job)
            if job["source"]:
                step("verify", lambda: verify(Path(job["source"])))
                preflight(job)
                if not job["promoted"]:
                    git(root_path(), "update-ref", "refs/operations/" + code, job["base"])
                    run(["git", "-C", root_path(), "merge", "--ff-only", job["target"]])
                    job["promoted"] = True
                    save(job_path(code), job)
                for folder in ("jarvis", "codex-worker", "bridge-kit", "grok-worker", "plugin/discord-orca"):
                    if any(f.startswith(folder + "/") for f in job["files"]) and (root_path() / folder / "package.json").exists():
                        step("dependencies:" + folder, lambda folder=folder: run(["bun", "install", "--ignore-scripts"], cwd=root_path() / folder))
            for name in job["launchd"]:
                step("launchd:" + name, lambda name=name: reload_launchd(name, job_path(code).parent / "backup"))
            checks = []
            for role in job["services"]:
                if "restart:" + role not in job["completed"]:
                    job.setdefault("restartedAt", {})[role] = time.time()
                step("restart:" + role, lambda role=role: run(["zsh", root_path() / "bin/model-restart.sh", role], timeout=240))
                if role == "리뷰어":
                    checks.append("리뷰어: 자비스 서버 새 프로세스 확인")
                else:
                    job["phase"] = "channel:" + role
                    checks.append(check_channel(role, job.get("restartedAt", {}).get(role, 0)))
            job.update(status="done", result="\n".join(checks) or "재시작 대상 없음")
        except Exception as exc:
            job.update(status="failed", result=f"{job.get('phase', 'preflight')} 실패: {exc}")
        job["finished"] = time.time()
        save(job_path(code), job)
        try:
            notify(job)
            job["reported"] = bool(job.get("chat"))
        except Exception:
            job["reported"] = False
        save(job_path(code), job)
        return job


def start(code, retry=False):
    with locked("apply.lock"):
        with locked("queue.lock"):
            job = read_job(code)
            expected = "failed" if retry else "planned"
            if job["status"] != expected:
                raise ValueError(f"작업 상태 {job['status']}: 중복 적용하지 않습니다")
            for file in directory().glob("*/job.json"):
                other = json.loads(file.read_text())
                if other["status"] in ("queued", "running"):
                    raise ValueError("다른 운영 작업이 진행 중입니다")
            preflight(job)
            folder = job_path(code).parent
            # Freeze this runner before it merges a newer version of itself.
            for name in ("operations.py", "config.py"):
                shutil.copy2(Path(__file__).parent / name, folder / name)
            env = dict(os.environ, **runtime_paths())
            job.update(status="queued", queued=time.time())
            save(job_path(code), job)
            try:
                with (folder / "apply.log").open("a") as log:
                    child = subprocess.Popen([sys.executable, str(folder / "operations.py"), "_run", code],
                        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                threading.Thread(target=child.wait, daemon=True).start()
                job["pid"] = child.pid
                save(job_path(code), job)
            except Exception:
                job.update(status="failed", result="운영 프로세스 시작 실패")
                save(job_path(code), job)
                raise
            return job


def status(code=None):
    files = [job_path(code)] if code else sorted(directory().glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    results = []
    for file in files:
        job = json.loads(file.read_text())
        if job["status"] in ("queued", "running") and time.time() - job.get("queued", 0) > 10:
            try:
                os.kill(job["pid"], 0)
            except (ProcessLookupError, KeyError):
                with locked("apply.lock"):
                    job = json.loads(file.read_text())
                    if job["status"] in ("queued", "running"):
                        job.update(status="failed", result="운영 프로세스가 중단되었습니다. 현재 커밋과 완료 단계를 확인한 뒤 retry 하세요.")
                        save(file, job)
        results.append(job)
    return results


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--thread")
    p.add_argument("--restart", action="append", choices=SERVICES, default=[])
    p.add_argument("--launchd", action="append", choices=JOBS, default=[])
    p.add_argument("--chat", help="명시된 Discord 채널/DM에 최종 결과 보고")
    for command in ("apply", "retry", "_run"):
        sub.add_parser(command).add_argument("id")
    sub.add_parser("status").add_argument("id", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "plan": result = plan(args.thread, args.restart, args.launchd, args.chat)
        elif args.command in ("apply", "retry"): result = start(args.id, args.command == "retry")
        elif args.command == "_run":
            # Parent releases its preparation lock immediately after recording pid.
            for attempt in range(50):
                try:
                    result = execute(args.id)
                    break
                except ValueError:
                    if attempt == 49: raise
                    time.sleep(0.1)
        else: result = status(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("status") == "failed" else 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())

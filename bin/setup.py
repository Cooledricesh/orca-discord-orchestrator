#!/usr/bin/env python3
"""Local setup and read-only diagnostics. Never starts bots or prints credentials."""
import argparse
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlencode

sys.dont_write_bytecode = True
from config import ENGINES, ROLES, TOOL_GATE_MODES, absolute, enabled_bots, load_routes, role_enabled, root_path, routes_path, runtime_paths, worker_entries


def run(args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{17,20}", value) is not None


def config_errors(data):
    errors = []
    for key in ("enabled", "bots", "review", "models"):
        if not isinstance(data.get(key, {}), dict):
            return [f"{key}: JSON 객체 필요"]
    if not isinstance(data.get("bots", {}).get("workers", []), list):
        return ["bots.workers: 배열 필요"]
    for key in ("guildId", "ownerUserId", "generalChannelId", "opsLogChannelId"):
        if not valid_id(data.get(key)):
            errors.append(f"{key}: 실제 Discord ID 필요")
    if data.get("generalChannelId") == data.get("opsLogChannelId"):
        errors.append("기획 채널과 운영 로그 채널은 분리해야 합니다")
    for role, value in data.get("enabled", {}).items():
        if role not in ROLES or not isinstance(value, bool):
            errors.append(f"enabled.{role}: 역할 이름과 true/false 확인")
    routes = data.get("routes", {})
    if not isinstance(routes, dict):
        return errors + ["routes: JSON 객체 필요"]
    projects = [(key, r) for key, r in routes.items() if isinstance(r, dict) and r.get("kind") != "lounge"]
    if role_enabled(data, "작업자") and not projects:
        errors.append("작업자용 프로젝트 라우트가 필요합니다")
    names, paths = set(), set()
    for channel, route in routes.items():
        if not isinstance(route, dict):
            errors.append(f"routes.{channel}: JSON 객체 필요"); continue
        if route.get("kind") == "lounge" and not role_enabled(data, "비전"):
            continue
        if not valid_id(channel):
            errors.append(f"routes.{channel}: 실제 채널 ID 필요")
        if channel in (data.get("generalChannelId"), data.get("opsLogChannelId")):
            errors.append(f"routes.{channel}: 기획/운영 채널과 프로젝트 채널은 분리해야 합니다")
        name = route.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in (".", ".."):
            errors.append(f"routes.{channel}.name: 경로 구분자 없는 프로젝트 이름 필요")
        elif name in names:
            errors.append(f"중복 프로젝트 이름: {name}")
        if isinstance(name, str): names.add(name)
        folder = route.get("path", "")
        if not isinstance(folder, str) or not Path(folder).is_absolute() or not Path(folder).is_dir():
            errors.append(f"routes.{channel}.path: 존재하는 절대 폴더 경로 필요")
            continue
        canonical = absolute(folder)
        if canonical in paths:
            errors.append(f"중복 프로젝트 경로: {canonical}")
        paths.add(canonical)
        if route.get("kind") != "lounge":
            rc, top = run(["git", "-C", folder, "rev-parse", "--show-toplevel"])
            head, _ = run(["git", "-C", folder, "rev-parse", "--verify", "HEAD"])
            if rc or head or absolute(top.strip()) != canonical:
                errors.append(f"{name}: 기준 커밋이 있는 Git 저장소 루트 필요")
        write_dir = route.get("writeDir")
        if write_dir and (not isinstance(write_dir, str) or Path(write_dir).is_absolute() or ".." in Path(write_dir).parts):
            errors.append(f"{name}.writeDir: 프로젝트 내부 상대 경로 필요")
    bot_names = []
    for role, name in enabled_bots(data):
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            errors.append(f"{role}: 유효한 봇 이름 필요")
        elif name in bot_names:
            errors.append(f"봇은 역할별로 달라야 합니다: {name}")
        bot_names.append(name)
    if role_enabled(data, "작업자") and not data.get("bots", {}).get("workers"):
        errors.append("bots.workers: 작업자 봇 한 개 이상 필요")
    seen = set()
    for i, entry in enumerate(worker_entries(data)):
        if entry["engine"] not in ENGINES:
            errors.append(f"bots.workers[{i}].engine: {' | '.join(ENGINES)} 만 허용")
        if not entry["name"]:
            errors.append(f"bots.workers[{i}]: 빈 봇 이름")
        elif entry["name"] in seen:
            errors.append(f"bots.workers: 중복 봇 이름 {entry['name']}")
        seen.add(entry["name"])
    if role_enabled(data, "비전") and sum(isinstance(r, dict) and r.get("kind") == "lounge" for r in routes.values()) != 1:
        errors.append("비전 활성 시 lounge 라우트는 정확히 하나 필요")
    gate = data.get("toolGate", {})
    if not isinstance(gate, dict):
        errors.append("toolGate: JSON 객체 필요")
    else:
        if "mode" in gate and gate["mode"] not in TOOL_GATE_MODES:
            errors.append(f"toolGate.mode: {' | '.join(TOOL_GATE_MODES)} 만 허용")
        if "threshold" in gate and (type(gate["threshold"]) not in (int, float) or not 0 < gate["threshold"] <= 1):
            errors.append("toolGate.threshold: 0 초과 1 이하 숫자 필요")
        if "notify" in gate and not isinstance(gate["notify"], bool):
            errors.append("toolGate.notify: true/false 필요")
    review = data.get("review", {})
    for key in ("maxConcurrent", "timeoutMin", "historyMaxMessages", "historyMaxChars", "channelThreadTtlHours", "gcDays"):
        if key in review and (type(review[key]) not in (int, float) or review[key] <= 0):
            errors.append(f"review.{key}: 양수 필요")
    return errors


def bot_fields(file):
    # No shell evaluation. The token is used only locally and never returned in a report.
    try:
        lines = file.read_text().splitlines()
    except FileNotFoundError:
        return {}
    fields = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key in ("DISCORD_APP_ID", "DISCORD_BOT_TOKEN"):
            fields[key] = value.strip().strip('"\'')
    return fields


def initialize(args):
    dest = routes_path()
    data = json.loads((root_path() / "routes.example.json").read_text())
    route = next(iter(data["routes"].values()))
    route["name"] = args.project_name
    route["path"] = absolute(args.project_path) if args.project_path else ""
    data["routes"] = {args.project_channel_id or "PROJECT_CHANNEL_ID": route}
    for key in ("guildId", "ownerUserId", "generalChannelId", "opsLogChannelId"):
        value = getattr(args, re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower())
        if value:
            data[key] = value
    # Pin the chosen account's file path, not its contents, for launchd sessions.
    data["codexAuthFile"] = runtime_paths({})["ORCH_CODEX_AUTH_FILE"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2); out.write("\n")
    print(f"4봇 로컬 설정 생성: {dest}")
    print("빈 프로젝트 경로와 Discord ID를 채운 뒤 doctor를 실행하세요.")


def doctor(args):
    checks = []
    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
    for binary in ("orca", "claude", "codex", "bun", "python3", "zsh", "git"):
        executable = os.environ.get({"orca": "ORCA_BIN", "codex": "CODEX_BIN", "claude": "CLAUDE_BIN"}.get(binary, "")) or binary
        found = shutil.which(executable)
        add(binary, found is not None, found or "설치/실행 경로 확인 필요")
    grok_workers = False
    try:
        data = load_routes()
    except ValueError as exc:
        add("routes.json", False, str(exc)); data = None
    if data is not None:
        errors = config_errors(data)
        add("프로젝트/Discord 설정", not errors, "; ".join(errors) or "유효한 로컬 설정")
        paths = runtime_paths(data)
        app_ids = set()
        for role, bot in enabled_bots(data):
            if not isinstance(bot, str) or not bot or bot in (".", "..") or "/" in bot or "\\" in bot:
                continue
            file = Path(paths["BOTS_DIR"]) / f"{bot}.env"
            fields = bot_fields(file)
            app_id = fields.get("DISCORD_APP_ID", "")
            complete = bool(fields.get("DISCORD_BOT_TOKEN")) and valid_id(app_id)
            private = file.is_file() and stat.S_IMODE(file.stat().st_mode) & 0o077 == 0
            unique = app_id not in app_ids
            if app_id: app_ids.add(app_id)
            add(f"{bot} ({role})", complete and private and unique, "로컬 설정 준비됨 (Discord 접속 미검증)" if complete and private and unique else f"토큰·앱 ID·권한 600·앱 ID 중복 확인: {file}")
        if role_enabled(data, "리뷰어"):
            auth = Path(paths["ORCH_CODEX_AUTH_FILE"])
            add("검수용 Codex 인증 파일", auth.is_file(), str(auth))
        packages = []
        if any(role_enabled(data, r) for r in ("상담역", "접수원", "작업자")): packages.append("plugin/discord-orca")
        if role_enabled(data, "리뷰어"): packages.append("jarvis")
        if role_enabled(data, "비전"): packages.append("codex-worker")
        grok_workers = role_enabled(data, "작업자") and any(e["engine"] == "grok" for e in worker_entries(data))
        if grok_workers:
            packages.append("grok-worker")
            found = shutil.which(os.environ.get("GROK_BIN") or "grok")
            add("grok", found is not None, found or "grok CLI 설치/실행 경로 확인 필요 (그록 작업자)")
            if data.get("grokSandbox"):
                # 프로필은 사용자 전역 파일에만 둘 수 있다. 안내만 하고 읽거나 쓰지 않는다.
                add("Grok 샌드박스 프로필", True, f"~/.grok/sandbox.toml 에 [{data['grokSandbox']}] 프로필 필요 (extends=\"workspace\", read_write 에 {root_path() / 'runs'} 와 {paths['STATE_DIR_ROOT']})")
        for package in packages:
            installed = (root_path() / package / "node_modules").is_dir()
            add(f"의존성: {package}", installed, "설치됨" if installed else f"cd {shlex.quote(str(root_path() / package))} && bun install --ignore-scripts")
        if any(role_enabled(data, r) for r in ("상담역", "접수원", "작업자")):
            marketplace_file = Path.home() / ".claude/plugins/known_marketplaces.json"
            try:
                markets = json.loads(marketplace_file.read_text())
                entry = markets.get("orca-local", {})
                installed = entry.get("installLocation") or entry.get("source", {}).get("path", "")
                ready = bool(installed) and absolute(installed) == str(root_path() / "plugin")
            except (OSError, ValueError, AttributeError):
                ready = False
            add("Claude 로컬 marketplace", ready, "현재 저장소 등록됨" if ready else f"claude plugin marketplace add {shlex.quote(str(root_path() / 'plugin'))}")
    if not args.offline:
        rc, output = run([os.environ.get("ORCA_BIN", "orca"), "status", "--json"])
        try:
            status = json.loads(output)
            ready = rc == 0 and status.get("ok") is True and status.get("result", {}).get("runtime", {}).get("reachable") is True
        except ValueError:
            ready = False
        add("Orca 런타임", ready, "실행 중" if ready else "Orca 앱 실행 및 CLI 연결 확인 필요")
        rc, output = run([os.environ.get("CLAUDE_BIN", "claude"), "auth", "status", "--json"])
        try:
            authenticated = rc == 0 and json.loads(output).get("loggedIn") is True
        except ValueError:
            authenticated = False
        add("현재 셸 Claude 로그인", authenticated, "로그인됨 (채널 기능은 별도 연결 검증 필요)" if authenticated else "claude auth login 필요")
        rc, _ = run([os.environ.get("CODEX_BIN", "codex"), "login", "status"])
        add("현재 셸 Codex 로그인", rc == 0, "로그인됨" if rc == 0 else "codex login 필요")
        if data and grok_workers:
            # 토큰·auth.json 은 읽지 않는다. `grok models` 첫 줄의 로그인 상태만 본다.
            rc, output = run([os.environ.get("GROK_BIN") or "grok", "models"], timeout=30)
            first = (output.strip().splitlines() or [""])[0]
            ok = rc == 0 and "logged in" in first.lower()
            add("현재 셸 Grok 로그인", ok, "로그인됨" if ok else "grok login 필요 (grok.com)")
        if data:
            targets = [str(root_path())] + [r.get("path", "") for r in data.get("routes", {}).values() if isinstance(r, dict)]
            for target in dict.fromkeys(targets):
                if target and Path(target).is_dir():
                    rc, _ = run([os.environ.get("ORCA_BIN", "orca"), "repo", "show", "--repo", f"path:{target}", "--json"])
                    add(f"Orca 저장소: {target}", rc == 0, "등록 확인" if rc == 0 else f"orca repo add --path {shlex.quote(target)} --json")
    result = {"ready": all(c["ok"] for c in checks), "checks": checks, "liveDiscordVerified": False}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"{'OK' if c['ok'] else 'TODO'} {c['check']}: {c['detail']}")
        print("Discord 연결·실제 모델 작업은 별도 점검이 필요합니다.")
    return 0 if result["ready"] else 1


def save_bot(args):
    data = load_routes()
    if args.bot not in [name for _, name in enabled_bots(data)]:
        raise ValueError("활성 역할의 봇 이름만 설정할 수 있습니다")
    if args.bot in (".", "..") or "/" in args.bot or "\\" in args.bot:
        raise ValueError("봇 이름에 경로 구분자를 사용할 수 없습니다")
    if not sys.stdin.isatty():
        raise ValueError("직접 연 터미널에서 실행하세요. 토큰은 숨김 입력으로 받습니다.")
    app_id = args.app_id or input("Discord Application ID: ").strip()
    if not valid_id(app_id):
        raise ValueError("유효한 Discord Application ID가 필요합니다")
    token = getpass.getpass("Discord Bot Token (표시되지 않음): ").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("비어 있지 않은 한 줄 토큰이 필요합니다")
    folder = Path(runtime_paths(data)["BOTS_DIR"])
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    dest = folder / f"{args.bot}.env"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(f"DISCORD_APP_ID={app_id}\nDISCORD_BOT_TOKEN={token}\n")
    print(f"저장 완료: {dest} (토큰은 출력하지 않습니다)")


def invites(args):
    data = load_routes()
    folder = Path(runtime_paths(data)["BOTS_DIR"])
    # Common chat permissions + explicit role-specific thread permissions.
    common = sum(1 << bit for bit in (6, 10, 11, 14, 15, 16, 38))
    for role, name in enabled_bots(data):
        app_id = bot_fields(folder / f"{name}.env").get("DISCORD_APP_ID", "")
        if not valid_id(app_id):
            print(f"{name}: 먼저 bot-env로 앱 ID를 저장하세요"); continue
        permissions = common
        if role in ("상담역", "접수원", "비전"): permissions |= 1 << 35
        if role == "작업자": permissions |= 1 << 34
        if args.manage_channels and role == "상담역": permissions |= 1 << 4
        query = {"client_id": app_id, "scope": "bot", "permissions": str(permissions)}
        if valid_id(data.get("guildId")): query.update(guild_id=data["guildId"], disable_guild_select="true")
        print(f"{name}: https://discord.com/oauth2/authorize?{urlencode(query)}")


def render_launchd(args):
    data = load_routes()
    paths = runtime_paths(data)
    dest = Path(args.output).expanduser().resolve() if args.output else Path(paths["STATE_DIR_ROOT"]) / "launchd"
    wanted = []
    if any(role_enabled(data, r) for r in ("상담역", "접수원", "비전")): wanted.append("leads-check")
    if role_enabled(data, "접수원"): wanted.append("접수원-restart")
    if role_enabled(data, "작업자"): wanted.append("sweep")
    if role_enabled(data, "리뷰어"): wanted.append("jarvis")
    env = dict(paths, HOME=str(Path.home()), PATH=os.environ.get("PATH", ""), LANG="en_US.UTF-8")
    env["PATH"] = os.pathsep.join(dict.fromkeys([str(Path.home() / ".local/bin"), str(Path.home() / ".bun/bin"), *env["PATH"].split(os.pathsep)]))
    env["ORCA_BIN"] = shutil.which(os.environ.get("ORCA_BIN", "orca")) or "orca"
    env["CODEX_BIN"] = shutil.which(os.environ.get("CODEX_BIN", "codex")) or "codex"
    env["CLAUDE_BIN"] = shutil.which(os.environ.get("CLAUDE_BIN", "claude")) or "claude"
    if os.environ.get("CODEX_HOME"): env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = []
    for job in wanted:
        template = root_path() / "launchd" / f"ai.orca.{job}.plist"
        with template.open("rb") as source: spec = plistlib.load(source)
        def expand(value):
            if isinstance(value, dict): return {k: expand(v) for k, v in value.items()}
            if isinstance(value, list): return [expand(v) for v in value]
            if isinstance(value, str):
                for key, replacement in paths.items(): value = value.replace(f"__{key}__", replacement)
                value = value.replace("__HOME__", str(Path.home()))
            return value
        spec = expand(spec)
        spec["EnvironmentVariables"] = env
        output = dest / template.name
        with output.open("wb") as out: plistlib.dump(spec, out, sort_keys=False)
        manifest.append(str(output))
        print(output)
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    Path(paths["STATE_DIR_ROOT"], "log").mkdir(parents=True, exist_ok=True)
    print("설정 파일만 생성했습니다. 실제 봇 연결 검증 후 LaunchAgents에 등록하세요.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="기존 파일을 덮어쓰지 않고 4봇 routes.json 생성")
    init.add_argument("--project-path")
    init.add_argument("--project-name", default="my-app")
    for key in ("guild-id", "owner-user-id", "general-channel-id", "ops-log-channel-id", "project-channel-id"):
        init.add_argument("--" + key)
    check = sub.add_parser("doctor", help="읽기 전용 준비 상태 점검")
    check.add_argument("--offline", action="store_true")
    check.add_argument("--json", action="store_true")
    bot = sub.add_parser("bot-env", help="사용자 터미널에서 토큰 숨김 입력")
    bot.add_argument("bot")
    bot.add_argument("--app-id")
    invite = sub.add_parser("invites", help="봇별 Discord 초대 URL 생성")
    invite.add_argument("--manage-channels", action="store_true")
    launchd = sub.add_parser("launchd", help="현재 경로와 활성 역할로 plist 생성 (등록하지 않음)")
    launchd.add_argument("--output")
    args = parser.parse_args()
    return {"init": initialize, "doctor": doctor, "bot-env": save_bot, "invites": invites, "launchd": render_launchd}[args.command](args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        sys.exit(1)

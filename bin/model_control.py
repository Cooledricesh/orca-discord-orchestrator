"""Owner-only, event-driven model control. No LLM calls; stdin is a Discord envelope."""
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.request

from config import load_routes, root_path, routes_path, runtime_paths, role_enabled
from failure_hook import read_json, save

ROLES = {"프라이데이": "상담역", "해피": "접수원", "마크": "작업자", "마크1": "작업자", "자비스": "리뷰어"}
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "fable")
HELP = ("프라이데이 DM 전용 · 모델 호출 없이 처리\n"
        "`!모델` 설정 조회 / `!모델 목록` 선택지\n"
        "`!모델 <봇> <모델> [effort]` 변경 제안\n"
        "`!모델 확인 <코드>` 적용 / `!모델 취소` 취소\n"
        "예: `!모델 해피 sonnet medium`\n"
        "Claude 봇은 Claude 모델만, 자비스는 Codex 모델만 선택 가능합니다. "
        "모델 변경으로 계정 사용량 제한이 해제되지는 않습니다.")


def directory():
    return Path(runtime_paths()["STATE_DIR_ROOT"]) / "model-control"


def catalog():
    path = Path(runtime_paths()["STATE_DIR_ROOT"]) / "jarvis/codex-home/models_cache.json"
    return {m["slug"]: m for m in read_json(path).get("models", []) if isinstance(m.get("slug"), str)}


def selection(cfg, role):
    models = cfg.get("models", {})
    if role == "리뷰어":
        value = models.get(role, {})
        return [value.get("model", ""), value.get("effort", "")]
    return [models.get(role, ""), models.get(role + "Effort", "")]


def set_selection(cfg, role, model, effort):
    models = cfg.setdefault("models", {})
    if role == "리뷰어":
        models.setdefault(role, {}).update(model=model, effort=effort)
    else:
        models[role], models[role + "Effort"] = model, effort


def validate(role, model, effort):
    if role == "리뷰어":
        models = catalog()
        if model and model not in models:
            raise ValueError("자비스 모델은 `!모델 목록`의 Codex 모델 또는 default를 선택하세요.")
        levels = {x["effort"] for x in models.get(model, {}).get("supported_reasoning_levels", [])}
        if not model and effort:
            raise ValueError("default 모델은 effort도 default로 지정하세요.")
        if effort and effort not in levels:
            raise ValueError("선택한 Codex 모델이 지원하지 않는 effort입니다. `!모델 목록`을 확인하세요.")
    else:
        if model not in CLAUDE_MODELS:
            raise ValueError("프라이데이·해피·마크는 Claude 전용입니다: opus / sonnet / haiku / fable. GPT로 엔진을 바꾸는 기능은 아닙니다.")
        if effort not in ("", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("Claude effort: low / medium / high / xhigh / max / default")


def description(role):
    if role == "작업자":
        return "작업자 공통 기본값만 변경합니다. 실행 중·대기열·이전 세션 재개 작업은 유지하며 다음 새 작업부터 적용합니다."
    return "해당 봇을 재시작합니다. 진행 중 응답이 끊길 수 있고, 새 모델로 새 대화 컨텍스트를 시작합니다."


def report(cfg, state):
    lines = ["설정된 기본 모델 (실제 응답 성공/사용량 상태와는 별개):"]
    for name in ("프라이데이", "해피", "마크", "자비스"):
        model, effort = selection(cfg, ROLES[name])
        lines.append(f"• {name}: {model or 'CLI 기본값'} / effort={effort or 'CLI 기본값'}")
    if state.get("job"):
        lines.append("최근 변경: " + state["job"].get("result", state["job"]["status"]))
    return "\n".join(lines) + "\n\n" + HELP


def request(event):
    cfg = load_routes()
    if (os.environ.get("ORCA_ROLE") != "상담역" or event.get("authorId") != cfg.get("ownerUserId")
            or event.get("authorIsBot") is not False or event.get("isDM") is not True
            or not re.fullmatch(r"\d{16,22}", str(event.get("channelId", "")))):
        return {"text": "모델 관리는 소유자의 프라이데이 1:1 DM에서만 허용됩니다."}
    content = str(event.get("content", ""))
    if len(content) > 300 or "\n" in content or "\r" in content:
        return {"text": HELP}
    args = content.strip().split()
    if not args or args[0] not in ("!모델", "!model"):
        return {"text": HELP}
    directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory() / "lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load_routes()
        state = read_json(directory() / "state.json")
        job = state.get("job", {})
        # Recovery on the next command only, not a watchdog.
        if job.get("status") in ("queued", "running") and time.time() - job["created"] > 240:
            job.update(status="failed", result="이전 적용 결과를 확인하지 못했습니다. 기본 설정과 봇 상태를 확인하세요.")
            save(directory() / "state.json", state)
        if len(args) == 1:
            return {"text": report(cfg, state)}
        if args[1:] == ["목록"]:
            rows = ["Claude: " + ", ".join(CLAUDE_MODELS) + " (effort: low/medium/high/xhigh/max/default)", "Codex (로컬 카탈로그; 실제 계정 이용 가능 여부는 실행 시 확인):"]
            rows += [k + ": " + "/".join(x["effort"] for x in v.get("supported_reasoning_levels", [])) for k, v in catalog().items()]
            rows.append("자비스 default default: CLI 기본값으로 복원")
            return {"text": "\n".join(rows)[:1900]}
        if args[1:] == ["취소"]:
            state.pop("pending", None)
            save(directory() / "state.json", state)
            return {"text": "확인 대기 중인 제안을 취소했습니다. 이미 시작한 적용은 취소하지 않습니다."}
        if args[1] == "확인" and len(args) == 3:
            pending = state.get("pending", {})
            if (pending.get("code") != args[2] or pending.get("chat") != event["channelId"]
                    or pending.get("author") != event["authorId"] or time.time() > pending.get("expires", 0)):
                return {"text": "유효한 변경 제안이 없습니다. 다시 제안하세요 (확인 코드는 5분간 유효)."}
            if job.get("status") in ("queued", "running"):
                return {"text": "다른 모델 변경을 적용 중입니다. `!모델`로 결과를 확인하세요."}
            if cfg.get("models") != pending["baseline"]:
                return {"text": "제안 후 설정이 바뀌었습니다. 다시 제안해 주세요."}
            state["job"] = dict(pending, status="queued", created=time.time())
            state.pop("pending")
            save(directory() / "state.json", state)
            return {"text": "변경을 접수했습니다. 적용 결과를 이 DM에 알립니다.", "job": args[2]}
        if len(args) not in (3, 4) or args[1] not in ROLES:
            return {"text": HELP}
        role = ROLES[args[1]]
        if not role_enabled(cfg, role):
            return {"text": "비활성 역할입니다. 모델 명령으로 역할을 활성화하지는 않습니다."}
        if job.get("status") in ("queued", "running"):
            return {"text": "모델 변경을 적용 중입니다. `!모델`로 결과를 확인하세요."}
        model = "" if args[2] == "default" else args[2]
        effort = args[3] if len(args) == 4 else selection(cfg, role)[1]
        effort = "" if effort == "default" else effort
        validate(role, model, effort)
        code = secrets.token_hex(4)
        state["pending"] = dict(code=code, role=role, model=model, effort=effort,
                                chat=event["channelId"], author=event["authorId"],
                                expires=time.time() + 300, baseline=copy.deepcopy(cfg.get("models", {})))
        save(directory() / "state.json", state)
        return {"text": f"변경 제안: {args[1]} → {model or 'CLI 기본값'} / effort={effort or 'CLI 기본값'}\n"
                        f"{description(role)}\n계정 한도는 그대로이며 모델 호출 성공을 보장하지 않습니다.\n"
                        f"적용: `!모델 확인 {code}` (5분 이내) / 취소: `!모델 취소`"}


def notify(chat, text):
    cfg = load_routes()
    envfile = Path(runtime_paths(cfg)["BOTS_DIR"]) / (cfg["bots"]["상담역"] + ".env")
    token = ""
    for line in envfile.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('\"\'')
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{chat}/messages",
        data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Authorization": "Bot " + token, "Content-Type": "application/json", "User-Agent": "orchestrator-model-control/1.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()


def apply_job(code):
    # This process is detached before a self-restart can kill the Discord plugin.
    with open(directory() / "apply.lock", "a") as apply_lock:
        try:
            fcntl.flock(apply_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with open(directory() / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(directory() / "state.json")
            job = state.get("job", {})
            if job.get("code") != code or job.get("status") != "queued":
                return
            cfg = load_routes()
            if (cfg.get("models") != job["baseline"] or time.time() > job["expires"]
                    or cfg.get("ownerUserId") != job["author"] or not role_enabled(cfg, job["role"])):
                job.update(status="failed", result="설정/소유자 변경 또는 제안 만료로 취소됨. 다시 제안하세요.")
                save(directory() / "state.json", state)
                return
            validate(job["role"], job["model"], job["effort"])
            job.update(status="running")
            save(directory() / "state.json", state)
            save(directory() / "routes-before.json", cfg)
            set_selection(cfg, job["role"], job["model"], job["effort"])
            # Force a new reviewer conversation on next turn; keep old transcripts.
            if job["role"] == "리뷰어":
                cfg["models"]["리뷰어"]["revision"] = code
            save(routes_path(), cfg)
        try:
            if job["role"] != "작업자":
                command = ["zsh", str(root_path() / "bin/model-restart.sh"), job["role"]]
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, timeout=180, check=False)
                if result.returncode:
                    lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
                    raise RuntimeError(lines[-1][:300] if lines else "restart failed")
            status = "done"
            message = ("✅ 모델 설정 적용: " + job["role"] + " → " + (job["model"] or "CLI 기본값")
                       + " / " + (job["effort"] or "CLI 기본 effort") + "\n"
                       + ("다음 새 작업부터 적용됩니다. 기존 작업/대기열/재개 세션은 유지합니다." if job["role"] == "작업자"
                          else "봇 프로세스 재시작을 확인했습니다. 모델 응답 성공·사용량 복구까지 확인한 것은 아닙니다."))
        except Exception as error:
            status = "failed"
            message = ("⚠️ 모델 설정은 저장됐지만 봇 재시작을 확인하지 못했습니다. `!모델`로 확인하고 같은 설정으로 다시 변경을 요청할 수 있습니다."
                       + ("\n원인: " + str(error) if isinstance(error, RuntimeError) else ""))
        with open(directory() / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(directory() / "state.json")
            if state.get("job", {}).get("code") == code:
                state["job"].update(status=status, result=message)
                save(directory() / "state.json", state)
        try:
            notify(job["chat"], message)
        except Exception:
            pass  # Durable result is available via !모델; never print credentials.


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--apply" and re.fullmatch("[a-f0-9]{8}", sys.argv[2]):
        try:
            apply_job(sys.argv[2])
        except Exception:
            # Never lose the failure silently or expose token-bearing exception text.
            with open(directory() / "lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = read_json(directory() / "state.json")
                if state.get("job", {}).get("code") == sys.argv[2]:
                    state["job"].update(status="failed", result="설정 저장/적용을 완료하지 못했습니다. 관리 세션에서 확인이 필요합니다.")
                    save(directory() / "state.json", state)
    else:
        try:
            answer = request(json.load(sys.stdin))
        except ValueError as exc:
            answer = {"text": str(exc)}
        except Exception:
            answer = {"text": "모델 관리 설정을 읽지 못했습니다. 관리 세션에서 확인이 필요합니다."}
        print(json.dumps(answer, ensure_ascii=False))

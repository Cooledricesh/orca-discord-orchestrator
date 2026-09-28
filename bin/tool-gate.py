#!/usr/bin/env python3
"""마크 Bash 툴 게이트 (PreToolUse, matcher Bash). Jev 분류기로 allow/ask/deny 를 판정한다.

- 마크 세션(ORCA_THREAD_ID)의 Bash 만 대상. 상시 세션·그록·자비스는 건너뛴다.
- 1차: 명백히 안전한 명령(읽기 전용 셸·git 조회·지정 운영 스크립트)은 Jev 없이 allow.
- 2차: 나머지는 OpenRouter Decisions API(Jev)에 State/Choice 로 묻는다. 최종 판정은 코드가 한다
  (확신도 < threshold 면 allow·deny 모두 ask 로 내린다).
- 키 없음·타임아웃·네트워크·응답 형식 오류는 fail-open(allow) 하고 기록에 error 를 남긴다.
- 모드: routes.json toolGate.mode — shadow(기본: 분리 자식이 판정·기록만, 훅은 즉시 반환) /
  enforce(동기 판정, deny 는 차단, ask 는 차단 + 스레드에 확인을 물으라는 사유) / off.
기록: $STATE_DIR_ROOT/tool-gate/<threadId>.jsonl. 키: $BOTS_DIR/openrouter.env (OPENROUTER_API_KEY=).
"""
import json, math, os, re, shlex, sys, threading, time, urllib.request
from datetime import datetime

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

API_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_TIMEOUT_S = 3.5   # 훅 timeout 5초 안: 파이썬 기동·기록 여유 포함
CHOICES = ("allow", "ask", "deny")
QUESTION_VERSION = "bash-gate-v1-20260928"

SAFE_CMDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "rg", "wc", "pwd", "echo", "printf", "which", "file",
    "stat", "du", "df", "tree", "sort", "uniq", "cut", "tr", "diff", "cmp", "jq", "date", "basename",
    "dirname", "realpath", "readlink", "true", "test", "[", "cd", "sed", "find", "ps", "whoami", "uname",
}
SAFE_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "describe",
            "merge-base", "cat-file", "shortlog"}
SAFE_GIT_LIST = {"stash": {"list", "show"}, "worktree": {"list"}, "remote": {"-v", "show", "get-url"}}
BRANCH_READ_ARGS = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--merged", "--no-merged", "--contains"}
FIND_BAD_ARGS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
OPS_SCRIPTS = {"post-result.sh", "finish-worker.sh"}
PUNCT = "();<>|&\n"
HARMLESS_REDIRECT = re.compile(r"\s\d?>&\d\b|\s(?:\d|&)?>>?\s*/dev/null\b")


def safe_by_rule(cmd: str, orch_root: str = "") -> bool:
    """True 면 Jev 없이 allow. 조금이라도 애매하면 False (Jev 로 보낸다)."""
    if not cmd.strip() or "`" in cmd or "$(" in cmd or "<(" in cmd or ">(" in cmd:
        return False
    try:
        lex = shlex.shlex(HARMLESS_REDIRECT.sub(" ", " " + cmd), posix=True, punctuation_chars=PUNCT)
        lex.whitespace, lex.whitespace_split = " \t\r", True  # 따옴표 밖 줄바꿈은 명령 구분자
        tokens = list(lex)
    except ValueError:
        return False
    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok and not tok.strip(PUNCT):  # 연산자 토큰 (따옴표 안 문자열은 여기 오지 않게 아래서 거른다)
            if set(tok) & set("<>()"):
                return False  # 리다이렉트·서브셸
            if segment and not safe_segment(segment, orch_root):
                return False
            segment = []
        else:
            segment.append(tok)
    return True


def safe_segment(words: list[str], orch_root: str) -> bool:
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
        words = words[1:]  # FOO=bar cmd
    if not words:
        return False
    head, args = words[0], words[1:]
    if os.path.basename(head) in OPS_SCRIPTS and os.path.dirname(head) in {"$ORCH_ROOT/bin", os.path.join(orch_root or "$ORCH_ROOT", "bin")}:
        return True
    if head in ("python", "python3"):
        return bool(args) and (args[0] == "-c" or args[:2] in (["-m", "json.tool"], ["-m", "unittest"], ["-m", "pytest"]))
    if head == "git":
        while args and args[0] in ("-C", "--no-pager") :
            args = args[2:] if args[0] == "-C" else args[1:]
        if not args:
            return False
        sub, rest = args[0], args[1:]
        if sub == "branch":
            return all(a in BRANCH_READ_ARGS for a in rest)
        if sub in SAFE_GIT_LIST:
            return sub == "remote" and not rest or bool(rest) and rest[0] in SAFE_GIT_LIST[sub]
        return sub in SAFE_GIT
    if head == "sed":
        return not any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in args)
    if head == "find":
        return not (set(args) & FIND_BAD_ARGS)
    return head in SAFE_CMDS


def build_request(cmd: str, cwd: str, model: str) -> dict:
    state = {"state_version": QUESTION_VERSION, "shell_command": cmd[:4000],
             "working_directory": cwd, "actor": "autonomous coding agent working in a disposable git worktree",
             "protected": "~/.claude, routes.json, bot tokens, other worktrees, the original project folder, launchd jobs, running bot processes"}
    question = {"type": "choice", "instructions": {
        "question": "Should `shell_command` run without asking the owner first?",
        "scope": "Judge only the command's direct effect. Do not judge code quality or whether the task is useful.",
    }, "criteria": {
        "allow": {"what": "Reads, builds, tests, or edits files inside the working directory; local git commits; reversible actions.",
                  "not_for": "Anything touching protected resources, remote state, or deleting data outside the worktree."},
        "ask": {"what": "Plausibly legitimate but hard to reverse or outward-facing: git push, force/reset/clean, killing processes, launchctl, network installs, writes outside the worktree.",
                "not_for": "Plain reads or clearly destructive/exfiltrating commands."},
        "deny": {"what": "Destructive or dangerous: rm -rf on broad or home paths, piping downloads into a shell, printing or sending secrets, editing ~/.claude or bot tokens, killing all agent sessions.",
                 "not_for": "Ordinary development commands."},
    }}
    return {"model": model, "state": state, "questions": {"gate": question}}


def parse_response(data: dict) -> tuple[str, float]:
    """Jev 응답 → (choice, confidence). 형식이 어긋나면 ValueError."""
    try:
        answer = data["answers"]["gate"]
        choice, conf = answer["choice"], answer["confidence"]
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid_response") from exc
    if choice not in CHOICES or isinstance(conf, bool) or not isinstance(conf, (int, float)) \
            or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise ValueError("invalid_response")
    return choice, float(conf)


def decide(choice: str, conf: float, threshold: float) -> str:
    """권한은 코드가 소유한다: 확신이 낮으면 어느 쪽이든 ask."""
    return choice if choice == "ask" or conf >= threshold else "ask"


def load_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                name, _, value = line.strip().partition("=")
                if name.strip() == "OPENROUTER_API_KEY" and value.strip().strip("\"'"):
                    return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def call_jev(body: dict, key: str, timeout: float = JEV_TIMEOUT_S, opener=None) -> dict:
    """전체 소요(DNS 포함)를 timeout 으로 자른다. 넘으면 TimeoutError."""
    box: dict = {}

    def run():
        req = urllib.request.Request(API_URL, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
                box["data"] = json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001 — 전부 fail-open
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or isinstance(box.get("error"), TimeoutError):
        raise TimeoutError("timeout")
    if "error" in box:
        raise ConnectionError("api_error")
    return box.get("data") or {}


def evaluate(cmd: str, cwd: str, cfg: dict, key_file: str, orch_root: str = "", jev=call_jev) -> dict:
    """판정 한 건. 항상 dict 를 돌려주고 예외를 올리지 않는다."""
    started = time.monotonic()
    rec = {"verdict": None, "confidence": None, "source": "rule", "error": None}
    if safe_by_rule(cmd, orch_root):
        rec["decision"] = "allow"
    else:
        key = load_key(key_file)
        if not key:
            rec.update(decision="allow", source="fail-open", error="missing_key")
        else:
            try:
                choice, conf = parse_response(jev(build_request(cmd, cwd, cfg["model"]), key))
                rec.update(verdict=choice, confidence=round(conf, 4), source="jev",
                           decision=decide(choice, conf, cfg["threshold"]))
            except TimeoutError:
                rec.update(decision="allow", source="fail-open", error="timeout")
            except ValueError:
                rec.update(decision="allow", source="fail-open", error="invalid_response")
            except Exception:  # noqa: BLE001
                rec.update(decision="allow", source="fail-open", error="api_error")
    rec["ms"] = int((time.monotonic() - started) * 1000)
    return rec


def gate_config() -> dict:
    from config import TOOL_GATE_DEFAULTS, load_routes, tool_gate
    try:
        return tool_gate(load_routes())
    except Exception:
        return dict(TOOL_GATE_DEFAULTS)  # 설정 없음·형식 오류여도 기본(shadow)으로 기록은 남긴다


def write_log(state_root: str, thread: str, rec: dict) -> None:
    d = os.path.join(state_root, "tool-gate")
    os.makedirs(d, exist_ok=True)
    fd = os.open(os.path.join(d, f"{thread}.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as out:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")


def notify(thread: str, rec: dict) -> None:
    sd = os.environ.get("DISCORD_STATE_DIR")
    if not sd:
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("progress_hook", os.path.join(os.path.dirname(os.path.realpath(__file__)), "progress-hook.py"))
    progress_api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(progress_api)
    token = progress_api.read_token(os.path.join(sd, ".env"))
    if not token:
        return
    conf = "" if rec["confidence"] is None else f" {rec['confidence']:.2f}"
    cmd = rec["command"][:80].replace("`", "'")
    progress_api.api(token, "POST", f"/channels/{thread}/messages",
                     {"content": f"🛡 게이트(섀도) {rec['decision']}{conf} · `{cmd}`", "allowed_mentions": {"parse": []}})


def enforce_output(rec: dict) -> dict | None:
    if rec["decision"] == "allow":
        return None
    reason = {"deny": "툴 게이트가 이 명령을 차단했다. 다른 방법을 찾거나 소유자에게 스레드로 알린다.",
              "ask": "툴 게이트: 되돌리기 어려운 명령이다. 실행하지 말고 스레드에 소유자 확인을 요청한 뒤 답을 기다린다."}[rec["decision"]]
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def run_gate(ev: dict, thread: str, cfg: dict, env: dict) -> dict:
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    bots = env.get("BOTS_DIR") or os.path.expanduser("~/.claude/channels/bots")
    rec = evaluate(cmd, ev.get("cwd") or "", cfg, os.path.join(bots, "openrouter.env"), env.get("ORCH_ROOT", ""))
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "command": cmd[:300],
           "mode": cfg["mode"], **rec}
    state_root = env.get("STATE_DIR_ROOT") or os.path.join(env.get("ORCH_ROOT") or os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "state")
    try:
        write_log(state_root, thread, rec)
    except OSError:
        pass
    return rec


def main() -> None:
    thread = os.environ.get("ORCA_THREAD_ID")
    if not thread:
        return
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    if ev.get("hook_event_name") != "PreToolUse" or ev.get("tool_name") != "Bash":
        return
    cfg = gate_config()
    if cfg["mode"] == "off":
        return
    if cfg["mode"] == "enforce":
        out = enforce_output(run_gate(ev, thread, cfg, dict(os.environ)))
        if out:
            print(json.dumps(out, ensure_ascii=False))
        return
    # shadow: 훅은 즉시 반환, 판정·기록·알림은 분리된 자식이.
    if os.fork():
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        rec = run_gate(ev, thread, cfg, dict(os.environ))
        if cfg["notify"] and rec["decision"] != "allow":
            notify(thread, rec)
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 게이트 오류로 마크를 막지 않는다

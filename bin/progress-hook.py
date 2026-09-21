#!/usr/bin/env python3
"""Claude 봇 진행 표시·오류 알림 훅 (PreToolUse / Stop / StopFailure).

- PreToolUse: 대상 chat 에 `⏳ N · <도구> <대상>` 진행 메시지를 올리고(턴의 첫 도구 호출), 이후는 5초 스로틀로 edit.
  첫 호출 때 typing 유지 루프(8초마다 typing, 턴이 끝나면 종료)를 분리 프로세스로 띄운다.
- Stop: 진행 메시지를 삭제한다 (결과는 모델이 별도 reply 로 올리므로 겹치지 않게). 루프는 상태 파일이 사라지면 멈춘다.
- StopFailure: 실패 상태를 기록하고 Discord에 알린 뒤 진행 표시를 종료한다. Stop에서 실패 상태를 해제한다.
모델 컨텍스트에는 아무것도 돌려주지 않는다 (stdout 없음, exit 0). 실패는 전부 무시한다.
대상 chat: 마크는 ORCA_THREAD_ID, 상시 세션(ORCA_ROLE)은 플러그인이 DISCORD_ACTIVITY_FILE 에 적는 `<ISO> <chatId>` 의 chatId.
토큰은 DISCORD_STATE_DIR/.env. 턴 경계는 DISCORD_ACTIVITY_FILE 의 내용으로 판정한다.
상태: state/threads/<thread>.progress.json 또는 state/leads/<역할>.progress.json (lock: .progress.lock)
"""
import fcntl, json, os, sys, time, urllib.request
import failure_hook

THROTTLE_S = 5
MAX_LEN = 120
API = "https://discord.com/api/v10"


TYPING_S = 8
TYPING_MAX_S = 3 * 3600


def main() -> None:
    sd = os.environ.get("DISCORD_STATE_DIR")
    root = os.environ.get("ORCH_ROOT") or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    state = os.environ.get("STATE_DIR_ROOT") or os.path.join(root, "state")
    thread, role = os.environ.get("ORCA_THREAD_ID"), os.environ.get("ORCA_ROLE")
    if thread:
        base, chat = os.path.join(state, "threads", thread), thread
    elif role:
        base, chat = os.path.join(state, "leads", role), activity_chat()
    else:
        return
    if not sd:
        return
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    kind = ev.get("hook_event_name")
    if kind == "PreToolUse":
        if not chat:
            return
        text = describe(ev)
        if not text:
            return
    elif kind in ("Stop", "StopFailure"):
        text = None
    else:
        return
    sequence, pid = time.time_ns(), failure_hook.read_pid(base)
    # 훅은 즉시 반환. 실제 REST 는 분리된 자식이 한다 (모델을 막지 않는다).
    if os.fork():
        return
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        failure_hook.handle(base, chat, sd, ev, sequence, pid, api, read_token)
    except Exception:
        pass
    try:
        work(base, chat, sd, kind, text)
    except Exception:
        pass
    os._exit(0)


def describe(ev: dict) -> str | None:
    name = ev.get("tool_name") or ""
    inp = ev.get("tool_input") or {}
    cwd = ev.get("cwd") or ""
    if "discord" in name.lower():  # reply/react/edit_message 는 스레드에 그대로 보인다
        return None
    short = name.split("__")[-1]
    if name == "Bash":
        arg = "`" + (inp.get("command") or "").replace("`", "'") + "`"
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        arg = os.path.relpath(p, cwd) if cwd and p.startswith("/") else p
    elif name in ("Grep", "Glob"):
        arg = inp.get("pattern") or ""
    elif name in ("Agent", "Task"):
        arg = inp.get("description") or ""
    elif name in ("WebFetch", "WebSearch"):
        arg = inp.get("url") or inp.get("query") or ""
    else:
        arg = ""
    s = " ".join(f"{short} {arg}".split())
    return s if len(s) <= MAX_LEN else s[: MAX_LEN - 1] + "…"


def work(base: str, chat: str, sd: str, kind: str, text: str | None) -> None:
    state_f, lock_f = base + ".progress.json", base + ".progress.lock"
    token = read_token(os.path.join(sd, ".env"))
    if not token:
        return
    with open(lock_f, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = {}
        try:
            with open(state_f) as stream:
                st = json.load(stream)
        except Exception:
            pass
        if kind in ("Stop", "StopFailure"):
            if st.get("messageId"):
                api(token, "DELETE", f"/channels/{st.get('chat') or chat}/messages/{st['messageId']}")
            try:
                os.remove(state_f)
            except FileNotFoundError:
                pass
            return
        turn = activity_key()
        now = time.time()
        if st.get("turnKey") != turn:  # 새 턴: 이전 진행 메시지는 지우고 새로 시작
            if st.get("messageId"):
                api(token, "DELETE", f"/channels/{st.get('chat') or chat}/messages/{st['messageId']}")
            st = {"turnKey": turn, "chat": chat, "messageId": None, "n": 0, "lastEdit": 0}
        st["n"] = int(st.get("n") or 0) + 1
        body = {"content": f"⏳ {st['n']} · {text}", "allowed_mentions": {"parse": []}}
        if not st.get("messageId"):
            r = api(token, "POST", f"/channels/{chat}/messages", body)
            st["messageId"] = (r or {}).get("id")
            st["lastEdit"] = now
            json.dump(st, open(state_f, "w"))
            typing_loop(token, chat, state_f, turn, lk)   # 이 턴 동안 typing 유지
            return
        if now - float(st.get("lastEdit") or 0) >= THROTTLE_S:
            api(token, "PATCH", f"/channels/{chat}/messages/{st['messageId']}", body)
            st["lastEdit"] = now
        json.dump(st, open(state_f, "w"))


def typing_loop(token: str, chat: str, state_f: str, turn: str, lk) -> None:
    """Discord typing 은 10초면 꺼진다. 상태 파일이 살아 있고 같은 턴인 동안 8초마다 다시 켠다. 토큰·모델과 무관."""
    if os.fork():
        return
    lk.close()  # flock 은 fd 에 붙는다. 물려받은 채로 두면 루프가 사는 동안 다른 훅(edit·Stop)이 전부 막힌다.
    os.setsid()
    started = time.time()
    while time.time() - started < TYPING_MAX_S:
        try:
            if json.load(open(state_f)).get("turnKey") != turn:
                break
        except Exception:
            break
        api(token, "POST", f"/channels/{chat}/typing")
        time.sleep(TYPING_S)
    os._exit(0)


def activity_chat() -> str:
    parts = activity_key().split()
    return parts[1] if len(parts) > 1 else ""


def activity_key() -> str:
    f = os.environ.get("DISCORD_ACTIVITY_FILE")
    try:
        return open(f).read().strip() if f else ""
    except Exception:
        return ""


def read_token(path: str) -> str:
    try:
        for line in open(path):
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


def api(token: str, method: str, ep: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + ep, data=data, method=method)
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "orchestrator-progress-hook/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception:
        return None


if __name__ == "__main__":
    main()

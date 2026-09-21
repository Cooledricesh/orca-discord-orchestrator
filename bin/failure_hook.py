"""Event-only Claude failure state and Discord notices; no polling or model calls."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile


REASONS = {
    "rate_limit": "사용량 제한",
    "overloaded": "모델 서버 혼잡",
    "authentication_failed": "인증 실패",
    "oauth_org_not_allowed": "조직 접근 권한 없음",
    "account_on_hold": "계정 사용 중지",
    "billing_error": "결제/크레딧 오류",
    "invalid_request": "잘못된 API 요청",
    "model_not_found": "모델을 찾을 수 없음",
    "server_error": "모델 서버 오류",
    "max_output_tokens": "응답 길이 한도 도달",
    "cloud_credential_error": "클라우드 인증 오류",
    "unknown": "모델 API 오류",
}
# Extract only a time/date/zone, never forward arbitrary API text or credentials.
RESET = re.compile(
    r"\bresets?\s+(?:(?:on|at)\s+)?"
    r"((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+)?"
    r"(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:am|pm)?"
    r"(?:\s*\((?:UTC|GMT|[A-Za-z_]+/[A-Za-z_]+)(?:[+-]\d{1,2}(?::\d{2})?)?\))?)",
    re.IGNORECASE,
)


def read_json(path):
    try:
        with open(path) as stream:
            data = json.load(stream)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_pid(base):
    try:
        return Path(base + ".pid").read_text().strip()
    except OSError:
        return ""


def save(path, data):
    fd, tmp = tempfile.mkstemp(prefix=".failure-", dir=Path(path).parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(tmp, path)  # mkstemp is 0600; readers never see partial JSON.
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def summary(state):
    reason = REASONS.get(state.get("error"), REASONS["unknown"])
    return f"응답 불가 — {reason} · 재개 예정: {state.get('reset') or '제공되지 않음'}"


def handle(base, chat, sd, event, sequence, pid, api, read_token):
    """Called in the detached hook child. Serialize only events for this bot."""
    kind = event.get("hook_event_name")
    if kind not in ("Stop", "StopFailure"):
        return
    root = os.environ.get("ORCH_ROOT") or str(Path(__file__).resolve().parent.parent)
    routes = read_json(os.environ.get("ROUTES_FILE") or os.path.join(root, "routes.json"))
    role = os.environ.get("ORCA_ROLE", "")
    bot = routes.get("bots", {}).get(role) or read_json(base + ".json").get("bot") or "작업 봇"
    path = base + ".failure.json"
    Path(base).parent.mkdir(parents=True, exist_ok=True)
    with open(base + ".failure.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A delayed child from a replaced process must not overwrite its successor.
        if pid != read_pid(base):
            return
        old = read_json(path)
        if sequence <= old.get("sequence", 0):
            return
        same_process = old.get("pid") == pid
        token = read_token(os.path.join(sd, ".env")) if sd else ""
        if kind == "Stop":
            # Keep a success tombstone to reject late-arriving failure children.
            save(path, {"active": False, "sequence": sequence, "pid": pid,
                        "session_id": event.get("session_id", "")})
            if same_process and old.get("active") and token:
                for channel in old.get("notified", []):
                    api(token, "POST", f"/channels/{channel}/messages", {
                        "content": f"✅ {bot}: 정상 응답 완료를 확인해 ‘응답 불가’ 상태를 해제했습니다.",
                        "allowed_mentions": {"parse": []},
                    })
            return
        error = event.get("error")
        error = error if error in REASONS else "unknown"
        message = str(event.get("last_assistant_message") or "")
        match = RESET.search(message) or RESET.search(str(event.get("error_details") or ""))
        reset = " ".join(match.group(1).split()) if match else ""
        same_failure = same_process and old.get("active") and old.get("error") == error and old.get("reset") == reset
        state = {"active": True, "error": error, "reset": reset, "pid": pid,
                 "session_id": event.get("session_id", ""), "sequence": sequence,
                 "notified": list(old.get("notified", [])) if same_failure else []}
        save(path, state)  # Status is accurate even if Discord is unavailable.
        if not token:
            return
        channels = dict.fromkeys([chat or routes.get("generalChannelId"), routes.get("opsLogChannelId")])
        for channel in channels:
            if not channel or not str(channel).isdigit() or str(channel) in state["notified"]:
                continue
            result = api(token, "POST", f"/channels/{channel}/messages", {
                "content": f"⛔ {bot}: {summary(state)}\n"
                           "요청을 읽었지만 모델 응답을 완료하지 못했습니다. "
                           "정상 응답이 확인되면 상태를 해제합니다.",
                "allowed_mentions": {"parse": []},
            })
            if result and result.get("id"):
                state["notified"].append(str(channel))
                save(path, state)


def status(base, pid):
    state = read_json(base + ".failure.json")
    if state.get("active") and state.get("pid") == pid and pid:
        print(summary(state))
        return 0
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--status":
        sys.exit(status(sys.argv[2], sys.argv[3]))
    sys.exit(2)

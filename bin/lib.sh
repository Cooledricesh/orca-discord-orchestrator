#!/bin/zsh
# 공용 헬퍼. 각 스크립트에서 `source "$(dirname "$0")/lib.sh"` 로 불러온다.
# 토큰은 절대 stdout/stderr 에 출력하지 않는다.
set -euo pipefail
export PATH="$HOME/.bun/bin:$PATH"   # launchd 에는 bun/codex 경로가 없다

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
ROUTES_FILE="${ROUTES_FILE:-$ORCH_ROOT/routes.json}"
STATE_DIR_ROOT="${STATE_DIR_ROOT:-$ORCH_ROOT/state}"
THREADS_DIR="$STATE_DIR_ROOT/threads"
POOL_FILE="$STATE_DIR_ROOT/pool.json"
POOL_LOCK="$STATE_DIR_ROOT/pool.lock"
BOTS_DIR="${BOTS_DIR:-$HOME/.claude/channels/bots}"
DISCORD_API="https://discord.com/api/v10"
PLUGIN_ID="discord-orca@orca-local"

mkdir -p "$THREADS_DIR" "$STATE_DIR_ROOT/log"

log()  { print -u2 -- "[$(basename "${ZSH_ARGZERO:-$0}")] $*"; }
warn() { log "WARN: $*"; }
die()  { log "ERROR: $*"; exit "${2:-1}"; }
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- routes.json ---------------------------------------------------------
# route_get <jsonpath 표현식>  예: route_get '["guildId"]'  /  route_get '["routes"]["123"]["path"]'
route_get() {
  python3 - "$ROUTES_FILE" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
try:
    v = eval("d" + sys.argv[2])
except (KeyError, IndexError, TypeError):
    sys.exit(1)
print(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False))
PY
}
guild_id()      { route_get '["guildId"]'; }
owner_id()      { route_get '["ownerUserId"]'; }
channels_flag() { route_get '["channelsFlag"]'; }
state_root()    { local r; r="$(route_get '["stateRoot"]')"; print -- "${r/#\~/$HOME}"; }
# 프로젝트 채널 목록 (한 줄에 하나). kind=lounge (비전) 는 제외.
project_channels() { python3 -c 'import json,sys;[print(k) for k,v in json.load(open(sys.argv[1]))["routes"].items() if v.get("kind") != "lounge"]' "$ROUTES_FILE"; }
lounge_channel()  { python3 -c 'import json,sys;[print(k) for k,v in json.load(open(sys.argv[1]))["routes"].items() if v.get("kind") == "lounge"]' "$ROUTES_FILE" | head -1; }
project_name_of() { route_get "[\"routes\"][\"$1\"][\"name\"]"; }
project_path_of() { route_get "[\"routes\"][\"$1\"][\"path\"]"; }
worker_bots()   { python3 -c 'import json,sys;[print(b) for b in json.load(open(sys.argv[1]))["bots"]["workers"]]' "$ROUTES_FILE"; }

# --- 봇 토큰 -------------------------------------------------------------
bot_env_file() { print -- "$BOTS_DIR/$1.env"; }
# bot_token <봇이름>  → 토큰 값을 stdout 으로 (변수 캡처 용도로만 쓸 것)
bot_token() {
  local f; f="$(bot_env_file "$1")"
  [[ -f "$f" ]] || die "봇 env 없음: $f"
  grep -E '^DISCORD_BOT_TOKEN=' "$f" | head -1 | cut -d= -f2- | tr -d '"\r'
}
# bot_app_id <봇이름>  → DISCORD_APP_ID (공개 값)
bot_app_id() {
  local f; f="$(bot_env_file "$1")"
  [[ -f "$f" ]] || die "봇 env 없음: $f"
  grep -E '^DISCORD_APP_ID=' "$f" | head -1 | cut -d= -f2- | tr -d '"\r'
}

# --- 프로세스 -------------------------------------------------------------
# pid_is <pid> <커맨드 부분 문자열>  → 살아 있고 커맨드라인에 문자열이 있으면 0 (재부팅 후 PID 재사용 오판 방지)
pid_is() { [[ -n "${1:-}" ]] && [[ "$(ps -o command= -p "$1" 2>/dev/null)" == *"$2"* ]]; }

# --- Discord REST --------------------------------------------------------
# discord_api <봇이름> <METHOD> <path(/channels/..)> [json-body]  → 응답 본문 stdout, HTTP 코드는 DISCORD_HTTP 변수
discord_api() {
  local bot="$1" method="$2" ep="$3" body="${4:-}"
  local token; token="$(bot_token "$bot")"
  local tmp; tmp="$(mktemp)"
  local code
  if [[ -n "$body" ]]; then
    code=$(curl --connect-timeout 5 --max-time 20 -s -o "$tmp" -w '%{http_code}' -X "$method" "$DISCORD_API$ep" \
      -H "Authorization: Bot $token" -H "Content-Type: application/json" -d "$body")
  else
    code=$(curl --connect-timeout 5 --max-time 20 -s -o "$tmp" -w '%{http_code}' -X "$method" "$DISCORD_API$ep" \
      -H "Authorization: Bot $token")
  fi
  DISCORD_HTTP="$code"
  cat "$tmp"; rm -f "$tmp"
  [[ "$code" == 2* ]]
}
# ops_log <봇> <한 줄>  → #운영-로그 (없으면 #프라이데이). 실패는 무시.
ops_log() {
  local ch; ch="$(route_get '["opsLogChannelId"]' 2>/dev/null || true)"; [[ -n "$ch" && "$ch" != null ]] || ch="$(route_get '["generalChannelId"]')"
  discord_api "$1" POST "/channels/$ch/messages" "$(python3 -c 'import json,sys;print(json.dumps({"content":sys.argv[1],"allowed_mentions":{"parse":[]}}))' "$2 ($(date +%H:%M))")" >/dev/null 2>&1 || true
}
discord_archive_thread() { discord_api "$1" PATCH "/channels/$2" '{"archived":true}' >/dev/null || log "스레드 보관 실패 HTTP $DISCORD_HTTP ($2)"; }

# --- 등록부 state/threads/<id>.json ---------------------------------------
registry_file() { print -- "$THREADS_DIR/$1.json"; }
registry_get() {
  local f; f="$(registry_file "$1")"
  [[ -f "$f" ]] || return 0
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));v=d.get(sys.argv[2],"");print(v if v is not None else "")' "$f" "$2"
}
registry_write() { print -r -- "$2" | python3 -c 'import json,sys;d=json.load(sys.stdin);open(sys.argv[1],"w").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n")' "$(registry_file "$1")"; }
# registry_update <threadId> key=value ...   (값은 문자열; "null" 은 null)
registry_update() {
  local f; f="$(registry_file "$1")"; shift
  python3 - "$f" "$@" <<'PY'
import json, sys, os
f = sys.argv[1]
d = json.load(open(f)) if os.path.exists(f) else {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    d[k] = None if v == "null" else v
open(f, "w").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
PY
}
# registry_list [status]  → "threadId<TAB>status<TAB>project<TAB>bot<TAB>terminalHandle<TAB>startedAt"
registry_list() {
  python3 - "$THREADS_DIR" "${1:-}" <<'PY'
import json, sys, os, glob
d, want = sys.argv[1], sys.argv[2]
for f in sorted(glob.glob(os.path.join(d, "*.json"))):
    try: r = json.load(open(f))
    except Exception: continue
    if want and r.get("status") != want: continue
    print("\t".join(str(r.get(k) or "") for k in ("threadId","status","project","bot","terminalHandle","startedAt")))
PY
}

# --- STATE_DIR 생성: mkstate <dir> <봇이름> <access-json> -----------------
mkstate() {
  local dir="$1" bot="$2" access="$3"
  mkdir -p "$dir"; chmod 700 "$dir"
  cp "$(bot_env_file "$bot")" "$dir/.env"; chmod 600 "$dir/.env"
  print -r -- "$access" | python3 -c 'import json,sys;d=json.load(sys.stdin);open(sys.argv[1],"w").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n")' "$dir/access.json"
}

# --- 플러그인 -------------------------------------------------------------
# 공식 경로 그대로: 세션 cwd 폴더에 project 스코프로 설치 + --dangerously-load-development-channels. (--plugin-dir 는 배너가 깨끗해도 채널 메시지가
# 주입되지 않는다 — 2026-09-10 실측, claude-code#43064 not planned.) 같은 폴더에서 사용자가 직접 켠 claude 도 플러그인을 로드하지만, 토큰이 없으면
# server.ts 가 실패 대신 도구 없이 대기하므로 Claude Code 의 15분 실패 캐시를 오염시키지 않는다.
channel_args() { print -r -- "--dangerously-load-development-channels $(channels_flag)"; }
# ensure_plugin <세션 cwd>  → 그 폴더에 project 설치가 없으면 설치
ensure_plugin() {
  local p="$1" ipf="$HOME/.claude/plugins/installed_plugins.json"
  python3 - "$ipf" "$p" "$PLUGIN_ID" <<'PY' && return 0
import json, sys, os
ipf, p_, pid = sys.argv[1:4]
if not os.path.exists(ipf): sys.exit(1)
for e in json.load(open(ipf)).get("plugins", {}).get(pid, []):
    if e.get("scope") == "project" and os.path.realpath(e.get("projectPath","")) == os.path.realpath(p_): sys.exit(0)
sys.exit(1)
PY
  log "플러그인 설치: $PLUGIN_ID @ $p"
  (cd "$p" && claude plugin install "$PLUGIN_ID" --scope project >/dev/null) || die "플러그인 설치 실패 ($p). 'claude plugin marketplace add ~/orchestrator/plugin' 먼저 확인"
}

# --- Orca -----------------------------------------------------------------
orca_ok() { orca status --json >/dev/null 2>&1; }
json_get() { python3 -c '
import json,sys
d=json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    d = d[int(k)] if isinstance(d,list) else d.get(k)
    if d is None: sys.exit(1)
print(d if not isinstance(d,(dict,list)) else json.dumps(d,ensure_ascii=False))' "$1" "$2"; }
shq() { print -r -- "${(qq)1}"; }

# accept_prompts <terminal handle> [timeout초]
# 기동 직후 뜨는 대화형 프롬프트(폴더 신뢰, 개발 채널 확인)를 자동 수락한다. 프롬프트(❯)가 뜨거나 타임아웃이면 종료.
accept_prompts() {
  local h="$1" limit="${2:-45}" t=0 tail
  while (( t < limit )); do
    sleep 3; t=$((t+3))
    tail="$(orca terminal read --terminal "$h" --limit 40 --json 2>/dev/null | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)["result"]["terminal"]; print(d.get("status",""),"|"," ".join(l for l in d["tail"] if l.strip()))
except Exception: print("")')"
    case "$tail" in
      exited*) return 1 ;;
      *"I trust this folder"*) orca terminal send --terminal "$h" --text $'\e[A' --json >/dev/null 2>&1; sleep 1; orca terminal send --terminal "$h" --text "" --enter --json >/dev/null 2>&1 ;;
      *"local development"*) orca terminal send --terminal "$h" --text "" --enter --json >/dev/null 2>&1 ;;
      *"bypass permissions on"*|*"❯"*) return 0 ;;
    esac
  done
  return 0
}

# plugin_child <claude pid>  → Discord 플러그인 자식(bun server.ts, claude 의 자식 또는 손자)이 붙어 있으면 0.
plugin_child() {
  local pid="$1" c pp
  [[ -n "$pid" ]] || return 1
  for c in $(pgrep -f 'bun server.ts' 2>/dev/null); do
    pp="$(ps -o ppid= -p "$c" 2>/dev/null | tr -d ' ')"
    [[ "$pp" == "$pid" || "$(ps -o ppid= -p "$pp" 2>/dev/null | tr -d ' ')" == "$pid" ]] && return 0
  done
  return 1
}
# channel_ok <terminal handle>  → 기동 배너에 "plugin not installed" 가 없으면 0. (MCP 는 붙었지만 채널 수신이 꺼진 상태를 잡는다. 2026-09-10 해피 사례)
channel_ok() { ! orca terminal read --terminal "$1" --screen --json 2>/dev/null | grep -q 'plugin not installed'; }
# plugin_ready <claude pid> <terminal handle> [timeout초]  → 플러그인 자식 + 채널 수신 둘 다 확인
plugin_ready() { wait_plugin "$1" "${3:-40}" && channel_ok "$2"; }
# wait_plugin <claude pid> [timeout초]  → 플러그인 자식이 붙을 때까지 기다린다. 못 붙으면 1.
wait_plugin() {
  local pid="$1" limit="${2:-40}" t=0
  while (( t < limit )); do plugin_child "$pid" && return 0; sleep 2; t=$((t+2)); done
  return 1
}

# 스레드 단위 직렬화: 같은 스레드의 spawn/finish 가 겹치지 않게 flock 아래에서 자기 자신을 재실행한다.
# 사용: 스크립트 앞부분에서 `with_thread_lock "$thread" "$0" "$@"`
with_thread_lock() {
  local thread="$1"; shift
  if [[ "${ORCA_WORKER_SPAWN_LOCK_PARENT:-}" != "$PPID" || -z "${ORCA_WORKER_SPAWN_LOCK_FD:-}" || ! "/dev/fd/${ORCA_WORKER_SPAWN_LOCK_FD:-none}" -ef "$THREADS_DIR/$thread.spawn.lock" ]]; then
    exec python3 "$ORCH_ROOT/bin/worker-spawn-lock.py" "$THREADS_DIR/$thread.spawn.lock" "$@"
  fi
  [[ -z "${ORCA_WORKER_SPAWN_LOCK_FD:-}" ]] || exec {ORCA_WORKER_SPAWN_LOCK_FD}>&-
  unset ORCA_WORKER_SPAWN_LOCK_FD ORCA_WORKER_SPAWN_LOCK_PARENT
}

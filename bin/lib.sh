#!/bin/zsh
# 공용 헬퍼. 각 스크립트에서 `source "$(dirname "$0")/lib.sh"` 로 불러온다.
# 토큰은 절대 stdout/stderr 에 출력하지 않는다.
set -euo pipefail
umask 077
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
# %x 는 이 source 파일. cwd 또는 설치 위치에 관계없이 루트를 찾는다.
ORCH_LIB_ROOT="${${(%):-%x}:A:h:h}"
config_env="$(python3 "$ORCH_LIB_ROOT/bin/config.py" shell)" || exit 1
eval "$config_env"  # config.py 가 shlex.quote 로 만든 export 문만 평가한다.
unset config_env
export ORCA_BIN="${ORCA_BIN:-${ORCA_CLI_COMMAND:-orca}}"
export CODEX_BIN="${CODEX_BIN:-codex}"
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export GROK_BIN="${GROK_BIN:-grok}"
THREADS_DIR="$STATE_DIR_ROOT/threads"
POOL_FILE="$STATE_DIR_ROOT/pool.json"
POOL_LOCK="$STATE_DIR_ROOT/pool.lock"
DISCORD_API="https://discord.com/api/v10"
PLUGIN_ID="discord-orca@orca-local"

ensure_state() { mkdir -p "$THREADS_DIR" "$STATE_DIR_ROOT/log"; }

log()  { print -u2 -- "[$(basename "${ZSH_ARGZERO:-$0}")] $*"; }
warn() { log "WARN: $*"; }
die()  { log "ERROR: $*"; exit "${2:-1}"; }
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- routes.json ---------------------------------------------------------
# route_get <jsonpath 표현식>  예: route_get '["guildId"]'  /  route_get '["routes"]["123"]["path"]'
route_get() {
  python3 "$ORCH_LIB_ROOT/bin/config.py" get "$1"
}
role_enabled() { python3 "$ORCH_LIB_ROOT/bin/config.py" enabled "$1"; }
guild_id()      { route_get '["guildId"]'; }
owner_id()      { route_get '["ownerUserId"]'; }
channels_flag() { route_get '["channelsFlag"]'; }
state_root()    { local r; r="$(route_get '["stateRoot"]' 2>/dev/null || print -- '~/.claude/channels')"; print -- "${r/#\~/$HOME}"; }
# 프로젝트 채널 목록 (한 줄에 하나). kind=lounge (비전) 는 제외.
project_channels() { python3 -c 'import json,sys;[print(k) for k,v in json.load(open(sys.argv[1]))["routes"].items() if v.get("kind") != "lounge"]' "$ROUTES_FILE"; }
lounge_channel()  { role_enabled 비전 || return 0; python3 -c 'import json,sys;print(next((k for k,v in json.load(open(sys.argv[1]))["routes"].items() if v.get("kind") == "lounge"),""))' "$ROUTES_FILE"; }
project_name_of() { route_get "[\"routes\"][\"$1\"][\"name\"]"; }
project_path_of() { route_get "[\"routes\"][\"$1\"][\"path\"]"; }
# worker_entries [engine] → "name<TAB>engine<TAB>model<TAB>effort" 줄. workers 항목은 문자열(=claude) 또는 {name, engine, model?, effort?}.
worker_entries() { role_enabled 작업자 || return 0; python3 "$ORCH_LIB_ROOT/bin/config.py" workers "$@"; }
worker_bots()   { worker_entries "$@" | cut -f1; }
# worker_field <봇> <2=engine|3=model|4=effort>  → 설정에 없는 봇이면 1
worker_field()  { local l; l="$(worker_entries | awk -F'\t' -v b="$1" '$1==b' | head -1)"; [[ -n "$l" ]] || return 1; print -r -- "$l" | cut -f"$2"; }
worker_engine() { worker_field "$1" 2; }

# grok_usage_line <sessionId>  → `grok usage` 요약 한 줄. grok.com 구독 인증이면 $ 는 API 환산값이다. 실패하면 1.
grok_usage_line() {
  [[ -n "${1:-}" ]] || return 1
  python3 - "$GROK_BIN" "$1" <<'PY'
import json, subprocess, sys
try:
    out = subprocess.run([sys.argv[1], "usage", sys.argv[2]], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    s = json.loads(out.stdout)["session"]
except Exception:
    sys.exit(1)
def k(n):
    n = int(n or 0)
    return str(n) if n < 1000 else f"{n/1000:.1f}k" if n < 10000 else f"{round(n/1000)}k"
cost = int(s.get("costUsdTicks") or 0) / 1e10
print(f"토큰 입력 {k(s.get('inputTokens'))}(캐시 {k(s.get('cachedReadTokens'))}) · 출력 {k(s.get('outputTokens'))} · ${cost:.3f}(환산) · {s.get('primaryModelId') or '?'} · {s.get('turnCount') or 0}턴")
PY
}

# --- 봇 토큰 -------------------------------------------------------------
bot_env_file() { print -- "$BOTS_DIR/$1.env"; }
# bot_token <봇이름>  → 토큰 값을 stdout 으로 (변수 캡처 용도로만 쓸 것)
bot_token() {
  local f; f="$(bot_env_file "$1")"
  [[ -f "$f" ]] || { warn "봇 env 없음: $f"; return 1; }
  grep -E '^DISCORD_BOT_TOKEN=' "$f" | head -1 | cut -d= -f2- | tr -d '"\r'
}
# bot_app_id <봇이름>  → DISCORD_APP_ID (공개 값)
bot_app_id() {
  local f; f="$(bot_env_file "$1")"
  [[ -f "$f" ]] || { warn "봇 env 없음: $f"; return 1; }
  grep -E '^DISCORD_APP_ID=' "$f" | head -1 | cut -d= -f2- | tr -d '"\r'
}
reviewer_app_id() {
  role_enabled 리뷰어 || return 0
  local b; b="$(route_get '["bots"]["리뷰어"]')" || return 0
  # 검수 봇을 아직 만들지 않았어도 다른 역할의 dry-run/기동을 막지 않는다.
  bot_app_id "$b" 2>/dev/null || true
}
ops_bot() {
  local role
  for role in 상담역 접수원 리뷰어; do
    if role_enabled "$role"; then route_get "[\"bots\"][\"$role\"]"; return; fi
  done
  worker_bots | head -1
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
# 검수 봇이 새 스레드의 멘션 자동완성에도 나타나도록 참가시킨다.
discord_join_reviewer() {
  role_enabled 리뷰어 || return 0
  local reviewer; reviewer="$(route_get '["bots"]["리뷰어"]')"
  discord_api "$reviewer" PUT "/channels/$1/thread-members/@me" >/dev/null || warn "검수 봇 스레드 참가 실패: $1"
}

# --- 등록부 state/threads/<id>.json ---------------------------------------
registry_file() { print -- "$THREADS_DIR/$1.json"; }
registry_get() {
  local f; f="$(registry_file "$1")"
  [[ -f "$f" ]] || return 0
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));v=d.get(sys.argv[2],"");print(v if v is not None else "")' "$f" "$2"
}
registry_write() { ensure_state; print -r -- "$2" | python3 -c 'import json,sys,os;d=json.load(sys.stdin);f=sys.argv[1];t=f+".tmp";open(t,"w").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n");os.replace(t,f)' "$(registry_file "$1")"; }
# registry_update <threadId> key=value ...   (값은 문자열; "null" 은 null)
registry_update() {
  local f; f="$(registry_file "$1")"; shift
  ensure_state
  python3 - "$f" "$@" <<'PY'
import json, sys, os
f = sys.argv[1]
d = json.load(open(f)) if os.path.exists(f) else {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    d[k] = None if v == "null" else v
t = f + ".tmp"
open(t, "w").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
os.replace(t, f)
PY
}
# registry_engine <threadId> → 등록부 engine (기존 기록처럼 없으면 claude)
registry_engine() { local e; e="$(registry_get "$1" engine)"; print -r -- "${e:-claude}"; }
# registry_list [status]  → "threadId<TAB>status<TAB>project<TAB>bot<TAB>terminalHandle<TAB>startedAt"
registry_list() {
  python3 - "$THREADS_DIR" "${1:-}" <<'PY'
import json, sys, os, glob
d, want = sys.argv[1], sys.argv[2]
rows = []
for f in glob.glob(os.path.join(d, "*.json")):
    try: r = json.load(open(f))
    except Exception: continue
    if want and r.get("status") != want: continue
    if not r.get("threadId"): continue
    rows.append(r)
for r in sorted(rows, key=lambda r: (r.get("queuedAt") or r.get("startedAt") or "", r["threadId"])):
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
  (cd "$p" && claude plugin install "$PLUGIN_ID" --scope project >/dev/null) || die "플러그인 설치 실패 ($p). claude plugin marketplace add '$ORCH_ROOT/plugin' 먼저 확인"
}

# --- Orca -----------------------------------------------------------------
orca() { command "$ORCA_BIN" "$@"; }
claude() { command "$CLAUDE_BIN" "$@"; }
orca_ok() { orca status --json >/dev/null 2>&1; }

# 상시 세션의 Orca 터미널 제목. 생성(lead-up.sh)과 재시작 검사(model-restart.sh)가 같은 기준을 쓰도록 여기 한 곳에서 만든다.
lead_title() { print -r -- "$1"; }
# terminal_is <핸들> <역할> [claude pid] → 시작 제목과 Claude가 갱신하는 봇 제목 모두 확인.
# 제목이 비어 있으면(orphaned) pid가 주어졌을 때만 그 claude 프로세스의 --name 으로 대신 확인한다. 실패 이유는 stderr.
terminal_is() {
  local info bot cmd=""
  info="$(orca terminal show --terminal "$1" --json)" || { log "terminal show failed: $1"; return 1; }
  bot="$(route_get "[\"bots\"][\"$2\"]" 2>/dev/null || true)"
  [[ -z "${3:-}" ]] || cmd="$(ps -o command= -p "$3" 2>/dev/null || true)"
  print -r -- "$info" | python3 -c 'import json,os,re,sys
t = json.load(sys.stdin)["result"]["terminal"]
root, role, bot, cmd = sys.argv[1:5]
def fail(reason):
    print("[terminal_is] " + reason, file=sys.stderr); sys.exit(1)
if t.get("agentIdentity") != "claude":
    fail("agentIdentity is %r, not claude" % t.get("agentIdentity"))
path = t.get("worktreePath") or ""
if not path or os.path.realpath(path) != os.path.realpath(root):
    fail("worktreePath %r != ORCH_ROOT" % path)
raw = (t.get("title") or "").strip()
if raw:
    title = re.sub(r"^[^\w]+", "", raw).strip()
    if title not in {role, bot} - {""}:
        fail("title %r is not %r" % (raw, bot or role))
    sys.exit(0)
if not cmd:
    fail("title is empty and recorded claude pid is not running")
argv = cmd.split()
named = any(a == "--name" and b == bot for a, b in zip(argv, argv[1:]))
if not bot or not any(os.path.basename(a) == "claude" for a in argv[:2]) or not named:
    fail("title is empty and pid command is not claude --name %s" % bot)' "$ORCH_ROOT" "$2" "$bot" "$cmd"
}
json_get() { python3 -c '
import json,sys
d=json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    d = d[int(k)] if isinstance(d,list) else d.get(k)
    if d is None: sys.exit(1)
print(d if not isinstance(d,(dict,list)) else json.dumps(d,ensure_ascii=False))' "$1" "$2"; }
shq() { print -r -- "${(qq)1}"; }

# Orca 터미널은 호출 셸의 환경을 상속한다고 가정하지 않는다. 비밀 값은 넣지 않는다.
# runtime_exports [grok]  → grok 브리지 터미널에는 GROK_BIN 도 넘긴다 (마크 커맨드는 기존 그대로).
runtime_exports() {
  local key keys=(ORCH_ROOT ROUTES_FILE STATE_DIR_ROOT BOTS_DIR ORCH_CODEX_AUTH_FILE ORCA_BIN CODEX_BIN CLAUDE_BIN)
  [[ "${1:-}" != grok ]] || keys+=(GROK_BIN)
  print -n -- 'export '
  for key in $keys PATH; do
    print -n -r -- "$key=$(shq "${(P)key}") "
  done
  [[ -z "${CODEX_HOME:-}" ]] || print -n -r -- "CODEX_HOME=$(shq "$CODEX_HOME") "
  print -- '; '
}
runtime_context() {
  print -r -- "운영 경로: ORCH_ROOT=$ORCH_ROOT / ROUTES_FILE=$ROUTES_FILE / STATE_DIR_ROOT=$STATE_DIR_ROOT / BOTS_DIR=$BOTS_DIR. 역할 문서의 환경변수 경로는 이 실제 경로로 치환한다. 비활성 역할에는 요청을 보내지 않는다."
}

# accept_prompts <terminal handle> [timeout초]
# 기동 직후 뜨는 대화형 프롬프트(폴더 신뢰, 개발 채널 확인)를 자동 수락한다. 프롬프트(❯)가 뜨거나 타임아웃이면 종료.
accept_prompts() {
  local h="$1" limit="${2:-45}" t=0 tail
  while (( t < limit )); do
    sleep 3; t=$((t+3))
    tail="$(orca terminal read --terminal "$h" --screen --limit 40 --json 2>/dev/null | python3 -c 'import sys,json
try:
  d=json.load(sys.stdin)["result"]["terminal"]; print(d.get("status",""),"|"," ".join(l for l in d["tail"] if l.strip()))
except Exception: print("")')"
    case "$tail" in
      exited*) return 1 ;;
      *"❯ No, exit"*) orca terminal send --terminal "$h" --text $'\e[A' --json >/dev/null 2>&1; sleep 1; orca terminal send --terminal "$h" --text "" --enter --json >/dev/null 2>&1 ;;
      *"I trust this folder"*) orca terminal send --terminal "$h" --text "" --enter --json >/dev/null 2>&1 ;;
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
  ensure_state
  if [[ "${ORCA_WORKER_SPAWN_LOCK_PARENT:-}" != "$PPID" || -z "${ORCA_WORKER_SPAWN_LOCK_FD:-}" || ! "/dev/fd/${ORCA_WORKER_SPAWN_LOCK_FD:-none}" -ef "$THREADS_DIR/$thread.spawn.lock" ]]; then
    exec python3 "$ORCH_ROOT/bin/worker-spawn-lock.py" "$THREADS_DIR/$thread.spawn.lock" "$@"
  fi
  [[ -z "${ORCA_WORKER_SPAWN_LOCK_FD:-}" ]] || exec {ORCA_WORKER_SPAWN_LOCK_FD}>&-
  unset ORCA_WORKER_SPAWN_LOCK_FD ORCA_WORKER_SPAWN_LOCK_PARENT
}

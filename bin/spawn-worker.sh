#!/bin/zsh
# 작업자 스폰 (마크=Claude, 그록=Grok 브리지).
#   spawn-worker.sh <channelId> <threadId> [--title "<t>|@file"] [--request-file <file>] [--request-message-id <id>]
#                   [--engine claude|grok] [--bot <이름>] [--model <id>] [--effort <low|medium|high|max>] [--new-worktree] [--resume] [--dry-run]
# --engine: 기본 claude. --bot: 그 봇만 빌린다 (엔진은 그 봇의 엔진). 모델/effort: --model > 봇 항목 model > Jev 난이도 매핑 > 엔진 기본값(작업자 / grok작업자).
# 난이도 라우팅: --model/--effort 가 없고 claude 엔진·새 작업이면 bin/classify-request.py 로 Jev 분류 (실패하면 기본값). 판정은 등록부 routeLevel/routeConfidence/routeSource.
# exit 0: 스폰됨 (stdout 마지막 줄 `bot=<bot> display=<표시이름> terminal=<handle> plugin=ok|missing`). missing 이면 마크가 스레드를 못 듣는다 (스레드·#운영-로그에 ⚠️ 게시됨)
# exit 3: 그 엔진의 풀 꽉 참 (등록부에 status=queued + engine 기록, 같은 엔진 finish-worker 가 다음에 자동 스폰)
# exit 1: 라우팅 불가 / 오류
# --resume: 이 스레드의 직전 Claude 세션(등록부 sessionId)을 같은 경로에서 `claude --resume` 으로 되살린다. 사용자가 명시했을 때만.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

channel="${1:-}"; thread="${2:-}"
[[ "$channel" == <-> && "$thread" == <-> ]] || die "usage: spawn-worker.sh <channelId> <threadId> [--title t|@file] [--request-file f] [--request-message-id id] [--model m] [--effort e] [--new-worktree] [--resume] [--dry-run]"
args=("$@"); shift 2
title=""; request=""; request_id=""; new_wt=1; resume=0; dry=0; sel_model=""; sel_effort=""; engine=""; sel_bot=""; def_model=""; def_effort=""
request_file=""; route_level=""; route_conf=""; route_source=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --title) title="${2:-}"; shift 2;;
    --request-file) request_file="${2:-}"; request="$(cat "$request_file")"; shift 2;;
    --request-message-id) request_id="${2:-}"; shift 2;;
    --model) sel_model="${2:-}"; shift 2;;
    --effort) sel_effort="${2:-}"; shift 2;;
    --engine) engine="${2:-}"; shift 2;;
    --bot) sel_bot="${2:-}"; shift 2;;
    # 대기열 재시도 전용: 대기 시점의 엔진 기본값 (봇 항목 model/effort 보다 우선순위가 낮다)
    --default-model) def_model="${2:-}"; shift 2;;
    --default-effort) def_effort="${2:-}"; shift 2;;
    # 대기열 재시도 전용: 대기 시점의 난이도 판정 (다시 분류하지 않는다)
    --route-level) route_level="${2:-}"; shift 2;;
    --route-confidence) route_conf="${2:-}"; shift 2;;
    --route-source) route_source="${2:-}"; shift 2;;
    --new-worktree) new_wt=1; shift;;
    --resume) resume=1; shift;;
    --dry-run) dry=1; shift;;
    *) die "알 수 없는 옵션: $1";;
  esac
done
role_enabled 작업자 || die "작업자 역할이 비활성화되어 있습니다"
if [[ -n "$sel_bot" ]]; then
  bot_engine="$(worker_engine "$sel_bot")" || die "작업자 봇이 아닙니다: $sel_bot"
  [[ -z "$engine" || "$engine" == "$bot_engine" ]] || die "$sel_bot 은 $bot_engine 엔진 봇입니다 (--engine $engine 과 다름)"
  engine="$bot_engine"
fi
# 재개는 등록부의 엔진 그대로 (기존 기록은 claude)
if (( resume )); then
  prev_engine="$(registry_engine "$thread")"
  [[ -z "$engine" || "$engine" == "$prev_engine" ]] || die "재개는 같은 엔진만 가능합니다 (이전 세션: $prev_engine)"
  engine="$prev_engine"
fi
engine="${engine:-claude}"
[[ "$engine" == claude || "$engine" == grok ]] || die "엔진은 claude|grok: $engine"
[[ -n "$(worker_bots "$engine")" ]] || die "$engine 엔진 작업자 봇이 routes.json bots.workers 에 없습니다"
(( dry )) || with_thread_lock "$thread" "$ORCH_ROOT/bin/spawn-worker.sh" "${args[@]}"
[[ "$title" == @* ]] && title="$(cat "${title#@}")"
[[ -z "$request_id" || "$request_id" == <-> ]] || die "request-message-id 는 숫자 ID"
(( resume )) && [[ -z "$title" ]] && title="$(registry_get "$thread" taskTitle)"
[[ -n "$title" ]] || title="task-$thread"

# 1. 라우팅
[[ "$(route_get "[\"routes\"][\"$channel\"][\"kind\"]" 2>/dev/null || true)" != lounge ]] || die "라운지 채널은 마크 대상이 아니다 (비전: bin/vision.sh)"
project="$(project_name_of "$channel" 2>/dev/null)" || die "routes.json 에 없는 채널: $channel"
proj_path="$(project_path_of "$channel")"
[[ -d "$proj_path" ]] || die "프로젝트 경로 없음: $proj_path"
proj_path="${proj_path:A}"
source_ref="$(git -C "$proj_path" rev-parse HEAD 2>/dev/null)" || die "프로젝트에 Git 기준 커밋이 필요합니다: $proj_path"
if (( ! resume )) && [[ "$proj_path" == "${ORCH_ROOT:A}" ]] && [[ -n "$(git -C "$proj_path" status --porcelain --untracked-files=normal)" ]]; then
  die "오케스트레이터 운영본에 미커밋 변경이 있습니다. 변경을 검토·커밋한 뒤 새 작업을 요청하세요. 오래된 HEAD로 시작하지 않습니다."
fi
write_dir="$(route_get "[\"routes\"][\"$channel\"][\"writeDir\"]" 2>/dev/null || true)"
owner="$(owner_id)"; guild="$(guild_id)"; plugin="$(channel_args)"
mkey=작업자; [[ "$engine" == claude ]] || mkey="${engine}작업자"
[[ -n "$def_model" ]] || def_model="$(route_get "[\"models\"][\"$mkey\"]" 2>/dev/null || true)"
[[ -n "$def_effort" ]] || def_effort="$(route_get "[\"models\"][\"${mkey}Effort\"]" 2>/dev/null || true)"
# 난이도 라우팅: 사용자 명시 > Jev > 기본값. 재개는 이전 판정, 대기열 재시도는 대기 시점 판정(--route-*)을 그대로 쓴다.
if (( resume )); then
  route_level="$(registry_get "$thread" routeLevel)"; route_conf="$(registry_get "$thread" routeConfidence)"; route_source="$(registry_get "$thread" routeSource)"
elif [[ -z "$route_source" ]]; then
  cls="$(python3 "$ORCH_ROOT/bin/classify-request.py" "${request_file:-/dev/null}" --project "$project" --engine "$engine" --model "$sel_model" --effort "$sel_effort" 2>/dev/null || true)"
  typeset -A rt; for kv in ${(z)cls}; do rt[${kv%%=*}]="${kv#*=}"; done
  route_level="${rt[level]:-}"; route_conf="${rt[confidence]:-}"; route_source="${rt[source]:-default}"
  (( dry )) && log "난이도 분류: ${cls:-(출력 없음)}"
  if [[ "$route_source" == jev ]]; then
    [[ -z "${rt[model]:-}" ]] || def_model="${rt[model]}"
    [[ -z "${rt[effort]:-}" ]] || def_effort="${rt[effort]}"
  fi
fi
model="${sel_model:-$def_model}"; effort="${sel_effort:-$def_effort}"
jarvis_app_id="$(reviewer_app_id)"
lounge="$(lounge_channel)"

# 2. 같은 스레드에 active 작업자가 있으면 거부
[[ "$(registry_get "$thread" status)" == "active" ]] && die "이미 active 작업자가 있는 스레드: $thread"
prev_path=""; prev_mode=""
if (( resume )); then
  sid="$(registry_get "$thread" sessionId)"; [[ -n "$sid" ]] || die "재개 불가: 등록부에 sessionId 없음 ($thread)"
  prev_path="$(registry_get "$thread" path)"; [[ -d "$prev_path" ]] || die "재개 불가: 이전 작업 경로 없음 ($prev_path)"
  prev_mode="$(registry_get "$thread" worktreeMode)"
  [[ "$prev_mode" == new ]] || die "공유 폴더의 이전 세션은 재개하지 않습니다. 인계 후 새 작업을 시작하세요."
  python3 "$ORCH_ROOT/bin/check-worktree.py" "$proj_path" "$prev_path" || die "이전 worktree 격리를 확인할 수 없습니다"
  [[ -n "$sel_model" ]] || model="$(registry_get "$thread" model)"
  [[ -n "$sel_effort" ]] || effort="$(registry_get "$thread" effort)"
  new_wt=0
else
  sid="$(uuidgen | tr 'A-Z' 'a-z')"
fi
# 새 작업은 항상 고유한 worktree. 같은 스레드의 재개는 위 스레드 락으로 직렬화한다.
leased=0; spawned=0; handle=""; prompt_file=""; sd=""; state_created=0
spawn_cleanup() {
  (( leased && ! spawned )) || return 0
  [[ -z "$handle" ]] || orca terminal close --terminal "$handle" --tab --json >/dev/null 2>&1 || true
  registry_update "$thread" "threadId=$thread" "channelId=$channel" "project=$project" status=failed "endedAt=$(now)" || true
  # 새 worktree 에 남은 터미널(Orca 첫 터미널 포함)도 닫는다. 공유 폴더는 위의 handle 만.
  python3 "$ORCH_ROOT/bin/worktrees.py" close-thread "$thread" || true
  "$ORCH_ROOT/bin/pool.sh" release "$thread" >/dev/null 2>&1 || true
  [[ -z "$prompt_file" ]] || rm -f "$prompt_file"
  if (( state_created )); then rm -f "$sd/.env" "$sd/access.json"; fi
  return 0
}
trap spawn_cleanup EXIT

# 3. 봇 대여 (dry-run 은 풀을 건드리지 않는다). worktree 보다 먼저: 풀이 꽉 차면 queued 로 끝난다.
if (( dry )); then bot="(dry)"
else
  orca_ok || die "Orca 런타임 응답 없음"
  rc=0; bot="$("$ORCH_ROOT/bin/pool.sh" lease "$thread" "$engine" "$sel_bot")" || rc=$?
  if (( rc == 3 )); then
    # model/effort 는 대기 시점 값. explicit* 가 false 면 재시도 때 봇 항목 model/effort 가 우선한다.
    registry_write "$thread" "$(python3 -c 'import json,sys;a=sys.argv;r=a[7]=="1";print(json.dumps(dict(threadId=a[1],channelId=a[2],project=a[3],path=(a[8] if r else a[4]),worktreeMode=(a[9] if r else None),bot=(a[17] or None),status="queued",taskTitle=a[5],newWorktree=a[6]=="1",resume=r,sessionId=(a[10] if r else None),queuedAt=a[11],currentRequest=a[12],requestMessageId=a[13],model=a[14],effort=a[15],engine=a[16],explicitModel=a[18]!="",explicitEffort=a[19]!="",routeLevel=a[20],routeConfidence=a[21],routeSource=a[22]),ensure_ascii=False))' \
      "$thread" "$channel" "$project" "$proj_path" "$title" "$new_wt" "$resume" "$prev_path" "$prev_mode" "$sid" "$(now)" "$request" "$request_id" "$model" "$effort" "$engine" "$sel_bot" "$sel_model" "$sel_effort" "$route_level" "$route_conf" "$route_source")"
    log "풀 꽉 참 ($engine) → queued"; exit 3
  fi
  (( rc == 0 )) || die "pool lease 실패 (rc=$rc)"
  leased=1
  registry_update "$thread" "threadId=$thread" "channelId=$channel" "project=$project" "projectPath=$proj_path" "bot=$bot" status=active "startedAt=$(now)" terminalHandle=
fi
# 봇 항목의 model/effort 는 --model/--effort 가 없고 재개가 아닐 때만 엔진 기본값보다 우선한다.
mbot="$bot"; (( dry )) && mbot="$sel_bot"
if (( ! resume )) && [[ -n "$mbot" ]]; then
  [[ -n "$sel_model" ]] || { m="$(worker_field "$mbot" 3 || true)"; model="${m:-$model}"; }
  [[ -n "$sel_effort" ]] || { m="$(worker_field "$mbot" 4 || true)"; effort="${m:-$effort}"; }
fi
# 배정 메시지용 한 단어 표시: (모델/effort · 자동|지정|기본)
case "$route_source" in jev) route_label=자동;; user) route_label=지정;; *) route_label=기본;; esac
route_tag="(${model:-기본}/${effort:-기본} · $route_label)"

# 4. 작업 디렉터리 (child worktree). 재개는 이전 경로 그대로.
worktree_mode="new"; work_path="$proj_path"
if (( resume )); then worktree_mode="$prev_mode"; work_path="$prev_path"; fi
if (( new_wt )); then
  wt_name="task-$thread-${sid[1,8]}"
  if (( dry )); then
    log "(dry-run) 독립된 작업 폴더 생성: $wt_name (base=$source_ref)"
    work_path="<new-worktree:$wt_name>"
  else
    out="$(orca worktree create --repo "path:$proj_path" --parent-worktree "path:$proj_path" --base-branch "$source_ref" --name "$wt_name" --json 2>&1)" || die "worktree 생성 실패 — 공유 폴더로 진행하지 않습니다. Orca 저장소 등록을 확인하세요."
    # Orca 가 worktree 와 함께 만든 첫 터미널(빈 셸)은 쓰지 않는다. 작업자 터미널은 아래에서 따로 만든다.
    st_handle="$(json_get "$out" result.startupTerminal.handle 2>/dev/null || true)"
    [[ -z "$st_handle" ]] || orca terminal close --terminal "$st_handle" --tab --json >/dev/null 2>&1 || warn "첫 터미널 닫기 실패: $st_handle"
    wp="$(json_get "$out" result.worktree.path 2>/dev/null || json_get "$out" result.path 2>/dev/null || true)"
    [[ -n "$wp" && -d "$wp" ]] || die "Orca가 유효한 worktree 경로를 반환하지 않았습니다"
    python3 "$ORCH_ROOT/bin/check-worktree.py" "$proj_path" "$wp" || die "worktree 격리 확인 실패 — 작업자를 실행하지 않습니다"
    work_path="${wp:A}"
    registry_update "$thread" "path=$work_path" worktreeMode=new "projectPath=$proj_path"
  fi
fi

# 5. STATE_DIR: 수신은 DISCORD_ONLY_CHATS(자기 스레드)로 제한, access 는 부모 채널 + 비전 라운지(인계 경로 게시용)
sd="$(state_root)/workers/$thread"
access="$(python3 -c 'import json,sys;g={"requireMention":False,"allowFrom":[sys.argv[2]]};groups={sys.argv[1]:g};
[groups.__setitem__(c,g) for c in sys.argv[4:] if c]
print(json.dumps(dict(dmPolicy="allowlist",allowFrom=[],groups=groups,ackReaction=sys.argv[3]),ensure_ascii=False))' "$channel" "$owner" "$(route_get '["emojis"]["working"]')" "$lounge")"

# 6. 초기 프롬프트
prompt_file="$THREADS_DIR/$thread.prompt.md"
(( dry )) && prompt_file="$(mktemp "${TMPDIR:-/tmp}/worker-prompt.XXXXXX")"
scope_note=""; [[ -n "$write_dir" ]] && scope_note="- 쓰기 허용 범위: \`$work_path/$write_dir\` 아래만."
finish_line="finish: \`$(shq "$ORCH_ROOT/bin/finish-worker.sh") $thread succeeded|failed|stopped\`"
[[ "$engine" == claude ]] || finish_line="finish: 브리지가 처리한다 (\"종료\"·\"중단\"). 직접 호출하지 않는다."
resume_first="스레드에 \`▶ 재개\` 라고 답하고 이어서 수행한다."; start_first="스레드에 \`▶ 시작 — $title\` 이라고 답하고 현재 요청대로 진행한다."
# 그록은 턴의 최종 텍스트만 게시되므로 시작 표시는 브리지가 올린다.
[[ "$engine" == claude ]] || { resume_first="\`▶ 재개\` 는 브리지가 이미 올렸다. 이어서 수행한다."; start_first="\`▶ 시작\` 은 브리지가 이미 올렸다. 현재 요청대로 진행한다."; }
if (( resume )); then
cat > "$prompt_file" <<EOF
# 세션 재개
이전 세션을 같은 스레드($thread)에서 이어서 실행한다. 이번 봇은 $bot. path: $work_path
ownerUserId: $owner
$(runtime_context)
jarvisAppId: ${jarvis_app_id:-(없음)} / visionLoungeChatId: ${lounge:-(없음)}
$finish_line
$scope_note
## 현재 사용자 요청
${request:-(없음. 필요한 지시는 스레드에서 사용자에게 확인한다.)}
- requestMessageId: ${request_id:-(없음)}
$resume_first
EOF
else
cat > "$prompt_file" <<EOF
# 작업자 기동 정보
$(runtime_context)
- threadId: $thread / channelId: $channel / guildId: $guild
- project: $project
- path: $work_path  (worktreeMode: $worktree_mode$( [[ "$work_path" != "$proj_path" ]] && print -n " / 원본 $proj_path" ))
- bot: $bot
- title: $title
- jarvisAppId: ${jarvis_app_id:-(없음)}
- ownerUserId: $owner
- visionLoungeChatId: ${lounge:-(없음)}
- $finish_line
$scope_note

## 현재 사용자 요청
${request:-(없음. 스레드에서 사용자에게 확인한다.)}
- requestMessageId: ${request_id:-(없음)}

## 첫 행동
$start_first 요청에 적힌 파일 경로만 읽는다. 스레드 이력을 조회하지 않는다.
EOF
fi

# 7. 실행 커맨드
effort_arg=""; [[ -n "$effort" ]] && effort_arg=" --effort $(shq "$effort")"
presence="🔧 $project / $title"
session_arg="--session-id $sid"; (( resume )) && session_arg="--resume $sid"
if [[ "$engine" == grok ]]; then
inner="$(runtime_exports grok) cd $(shq "$work_path") && print \$\$ > $(shq "$THREADS_DIR/$thread.pid") && DISCORD_STATE_DIR=$(shq "$sd") DISCORD_ACTIVITY_FILE=$(shq "$THREADS_DIR/$thread.activity") ORCA_THREAD_ID=$thread exec bun $(shq "$ORCH_ROOT/grok-worker/bridge.ts") $(shq "$THREADS_DIR/$thread.json")"
else
inner="$(runtime_exports) cd $(shq "$work_path") && print \$\$ > $(shq "$THREADS_DIR/$thread.pid") && DISCORD_STATE_DIR=$(shq "$sd") DISCORD_ACCESS_MODE=static DISCORD_ONLY_CHATS=$thread DISCORD_ACTIVITY_FILE=$(shq "$THREADS_DIR/$thread.activity") DISCORD_PRESENCE=$(shq "$presence") DISCORD_IGNORE_OTHER_BOT_MENTIONS=1 ORCA_THREAD_ID=$thread exec $(shq "$CLAUDE_BIN") --dangerously-skip-permissions $session_arg --model $(shq "$model")$effort_arg --name $(shq "$bot") $plugin --settings $(shq "$ORCH_ROOT/templates/progress-settings.json") --append-system-prompt-file $(shq "$ORCH_ROOT/roles/작업자.md") \"\$(cat $(shq "$prompt_file"))\""
fi
term_title="$bot $title"
# 등록부 추가 필드: 엔진은 항상, 브리지 설정(grok)은 grok 일 때만
reg_extra="$(python3 -c 'import json,sys;a=sys.argv;print(json.dumps(dict(engine=a[1],**(dict(guildId=a[2],ownerUserId=a[3],jarvisAppId=a[4],discordStateDir=a[5],rolesFile=a[6],ackEmoji=a[7],sandbox=a[8]) if a[1]=="grok" else {})),ensure_ascii=False))' \
  "$engine" "$guild" "$owner" "$jarvis_app_id" "$sd" "$ORCH_ROOT/roles/작업자-grok.md" "$(route_get '["emojis"]["working"]' 2>/dev/null || true)" "$(route_get '["grokSandbox"]' 2>/dev/null || true)")"

if (( dry )); then
  print -- "model=$model effort=$effort level=$route_level confidence=$route_conf source=${route_source:-default} route=$route_tag"
  [[ "$engine" == claude ]] || { print -- "--- registry extra ---"; print -r -- "$reg_extra" | python3 -m json.tool --no-ensure-ascii; }
  print -- "--- access.json ($sd) ---"; print -r -- "$access" | python3 -m json.tool
  print -- "--- prompt ($prompt_file) ---"; cat "$prompt_file"
  print -- "--- orca terminal create ---"
  print -r -- "orca terminal create --worktree path:$(shq "$work_path") --title $(shq "$term_title") --command $(shq "$inner") --json"
  rm -f "$prompt_file"; exit 0
fi

# 8. 등록부를 먼저 active 로 (terminalHandle 은 비움). baseRef 는 자비스의 diff 기준.
base_ref="$(git -C "$work_path" rev-parse HEAD 2>/dev/null || true)"
registry_write "$thread" "$(python3 -c 'import json,sys;a=sys.argv;print(json.dumps(dict(threadId=a[1],channelId=a[2],project=a[3],path=a[4],projectPath=a[5],worktreeMode=a[6],bot=a[7],terminalHandle="",promptFile=a[8],status="active",startedAt=a[9],endedAt=None,taskTitle=a[10],model=a[11],effort=a[12],baseRef=a[13],sessionId=a[14],resumed=a[15]=="1",currentRequest=a[16],requestMessageId=a[17],routeLevel=a[19],routeConfidence=a[20],routeSource=a[21] or "default",**json.loads(a[18])),ensure_ascii=False))' \
  "$thread" "$channel" "$project" "$work_path" "$proj_path" "$worktree_mode" "$bot" "$prompt_file" "$(now)" "$title" "$model" "$effort" "$base_ref" "$sid" "$resume" "$request" "$request_id" "$reg_extra" "$route_level" "$route_conf" "$route_source")"

# 9. 실행. 실패 경로 전체에서 등록부 failed + 풀 반납.
state_created=1
mkstate "$sd" "$bot" "$access"
[[ "$engine" != claude ]] || ensure_plugin "$work_path"
# 이전 기동의 runtime.json 이 남아 있으면 ready 로 오판한다
[[ "$engine" != grok ]] || rm -f "$THREADS_DIR/$thread.grok/runtime.json"
out="$(orca terminal create --worktree "path:$work_path" --title "$term_title" --command "$inner" --json)" || die "orca terminal create 실패: $out"
handle="$(json_get "$out" result.terminal.handle)" || die "terminal handle 없음: $out"
registry_update "$thread" "terminalHandle=$handle"
# 스레드 이름 접두어 🔧
tname="$(discord_api "$bot" GET "/channels/$thread" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("name",""))
except Exception: print("")')" || true
if [[ -n "$tname" && "$tname" != 🔧* ]]; then
  tname="${tname#✅ }"; tname="${tname#⏹ }"; tname="${tname#❌ }"
  discord_api "$bot" PATCH "/channels/$thread" "$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1][:100]}))' "🔧 $tname")" >/dev/null 2>&1 || true
fi
plugin_state=ok
if [[ "$engine" == grok ]]; then
  # 브리지가 게이트웨이에 붙으면 runtime.json status=ready. 40초 안에 안 되면 스레드에 알리고 풀을 반납한다 (설계 §6).
  rt="$THREADS_DIR/$thread.grok/runtime.json"; gst=""
  for i in {1..20}; do
    gst="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$rt" 2>/dev/null || true)"
    [[ "$gst" == ready || "$gst" == failed ]] && break
    sleep 2
  done
  if [[ "$gst" != ready ]]; then
    ops_log "$bot" "⚠️ $bot Grok 브리지가 준비되지 않음 (${gst:-응답 없음}) — 스레드 $thread 정리함"
    discord_api "$bot" POST "/channels/$thread/messages" "$(python3 -c 'import json,sys;print(json.dumps({"content":"⚠️ Grok 브리지가 준비되지 않았습니다 ("+sys.argv[1]+"). 작업자를 정리했습니다. 관리 세션에서 확인이 필요합니다.","allowed_mentions":{"parse":[]}}))' "${gst:-응답 없음}")" >/dev/null 2>&1 || true
    p="$(cat "$THREADS_DIR/$thread.pid" 2>/dev/null || true)"
    pid_is "$p" grok-worker/bridge.ts && kill -TERM "$p" 2>/dev/null || true
    rm -f "$THREADS_DIR/$thread.pid"
    die "Grok 브리지가 준비되지 않았습니다 (${gst:-응답 없음})"
  fi
else
accept_prompts "$handle" 60 || warn "터미널이 기동 중 종료됨: $handle"
if ! plugin_ready "$(cat "$THREADS_DIR/$thread.pid" 2>/dev/null)" "$handle" 40; then
  # 플러그인 자식이 없거나 배너에 "plugin not installed" 면 스레드 메시지를 못 받는다 → 터미널을 한 번 다시 띄운다 (같은 세션 ID, 프롬프트 재전달)
  warn "Discord 플러그인이 붙지 않음 → 터미널 재기동: $thread"
  kill -TERM "$(cat "$THREADS_DIR/$thread.pid" 2>/dev/null)" 2>/dev/null || true; sleep 3
  orca terminal close --terminal "$handle" --tab --json >/dev/null 2>&1 || true
  out="$(orca terminal create --worktree "path:$work_path" --title "$term_title" --command "$inner" --json)" || die "orca terminal create 실패: $out"
  handle="$(json_get "$out" result.terminal.handle)"; registry_update "$thread" "terminalHandle=$handle"
  accept_prompts "$handle" 60 || warn "터미널이 기동 중 종료됨: $handle"
  if plugin_ready "$(cat "$THREADS_DIR/$thread.pid" 2>/dev/null)" "$handle" 40; then plugin_state=ok
  else
    # 사용자가 보는 자리(스레드)에 알리고, 접수원도 출력으로 안다
    plugin_state=missing
    ops_log "$bot" "⚠️ $bot 은 떴지만 Discord 플러그인이 붙지 않음 — 스레드 $thread 수동 확인 필요"
    discord_api "$bot" POST "/channels/$thread/messages" "$(python3 -c 'import json;print(json.dumps({"content":"⚠️ 작업자는 떴지만 Discord 연결이 안 됐습니다. 이 스레드의 메시지를 못 받습니다. 관리 세션에서 확인이 필요합니다.","allowed_mentions":{"parse":[]}}))')" >/dev/null 2>&1 || true
  fi
fi
fi
mkdir -p "$ORCH_ROOT/runs/$project"
print -- "- $(date +%H:%M) $( (( resume )) && print -n resumed || print -n spawned ) $bot thread=$thread title=\"$title\" worktree=$worktree_mode" >> "$ORCH_ROOT/runs/$project/$(date +%Y-%m-%d).md"
disp="$(route_get "[\"botDisplay\"][\"$bot\"]" 2>/dev/null || print -- "$bot")"
ops_log "$bot" "▶ $disp 배정 — $project · $title $route_tag"
print -- "bot=$bot display=$disp terminal=$handle plugin=$plugin_state$( [[ "$engine" == claude ]] || print -n " engine=$engine" ) route=$route_tag"
spawned=1

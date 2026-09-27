#!/bin/zsh
# 작업자 결과 카드. post-result.sh <threadId> <succeeded|failed> "<제목>" "<본문(마크다운)>" ["<수정 파일 목록, 줄바꿈 구분>"]
set -euo pipefail
source "$(dirname "$0")/lib.sh"
thread="$1"; outcome="$2"; title="$3"; body="$4"; files="${5:-}"
bot="$(registry_get "$thread" bot)"; [[ -n "$bot" ]] || die "등록부에 bot 없음: $thread"
color=$([[ "$outcome" == succeeded ]] && print 3066993 || print 15158332)
owner="$(owner_id)"
# 그록의 usage 한 줄은 턴이 끝난 뒤 브리지가 이 카드에 덧붙인다 (grok usage 는 턴 종료 후에 저장된다).
payload="$(python3 - "$title" "$body" "$files" "$color" "$bot" "$owner" <<'PY'
import json,sys,datetime
title,body,files,color,bot,owner=sys.argv[1:7]
fields=[]
if files.strip(): fields.append({"name":"수정 파일","value":"\n".join("`"+f.strip()+"`" for f in files.splitlines() if f.strip())[:1024]})
print(json.dumps({"content":f"<@{owner}>","allowed_mentions":{"users":[owner]},
  "embeds":[{"title":title[:256],"description":body[:4000],"color":int(color),"fields":fields,
  "footer":{"text":bot},"timestamp":datetime.datetime.now(datetime.timezone.utc).isoformat()}]}))
PY
)"
discord_api "$bot" POST "/channels/$thread/messages" "$payload" >/dev/null && print -- "posted"

#!/bin/zsh
# 프로젝트 채널 메시지에 스레드를 만든다. 사용자 텍스트를 셸/JSON 리터럴에 끼워 넣지 않기 위한 스크립트 (JSON 인코딩은 python).
#   open-thread.sh <봇이름> <channelId> <messageId> <이름|@file>            기존 메시지에 스레드
#   open-thread.sh <봇이름> <channelId> new <이름|@file> <본문|@file>        본문을 먼저 게시하고 그 메시지에 스레드 (상담역이 넘길 때)
# stdout: threadId. 이름은 40자로 자른다.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
bot="${1:-}"; channel="${2:-}"; msg="${3:-}"; name="${4:-}"; content="${5:-}"
[[ -n "$bot" && -n "$channel" && -n "$msg" && -n "$name" ]] || die "usage: open-thread.sh <bot> <channelId> <messageId|new> <name|@file> [<content|@file>]"
[[ "$name" == @* ]] && name="$(cat "${name#@}")"
[[ "$content" == @* ]] && content="$(cat "${content#@}")"
jbody() { python3 -c 'import json,sys;print(json.dumps(dict(zip(sys.argv[1::2],sys.argv[2::2])),ensure_ascii=False))' "$@"; }
id_of() { python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])'; }

if [[ "$msg" == "new" ]]; then
  [[ -n "$content" ]] || die "new 에는 본문이 필요하다"
  out="$(discord_api "$bot" POST "/channels/$channel/messages" "$(jbody content "$content")")" || die "메시지 게시 실패: $out"
  msg="$(print -r -- "$out" | id_of)"
fi
name="$(python3 -c 'import sys;s=" ".join(sys.argv[1].split());print(s[:40] or "작업")' "$name")"
body="$(python3 -c 'import json,sys;print(json.dumps({"name":sys.argv[1],"auto_archive_duration":1440},ensure_ascii=False))' "$name")"
out="$(discord_api "$bot" POST "/channels/$channel/messages/$msg/threads" "$body")" || die "스레드 생성 실패: $out"
print -r -- "$out" | id_of

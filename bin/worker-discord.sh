#!/bin/zsh
# 그록 작업자용 Discord 소형 헬퍼 (grok 세션이 셸로 부른다). 봇은 등록부 bot, 스레드는 ORCA_THREAD_ID 로 고정한다.
#   worker-discord.sh react <messageId> <emoji>
# 먼저 스레드에서 메시지를 찾고, 없으면(404 Unknown Message) 등록부 channelId 에서 찾는다 (스레드 시작 메시지는 부모 채널에 있다).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
cmd="${1:-}"; mid="${2:-}"; emoji="${3:-}"
thread="${ORCA_THREAD_ID:-}"
[[ "$cmd" == react && "$mid" == <-> && -n "$emoji" && ${#emoji} -le 64 ]] || die "usage: worker-discord.sh react <messageId> <emoji>"
[[ "$thread" == <-> ]] || die "ORCA_THREAD_ID 가 없습니다 (작업자 세션에서만 사용)"
bot="$(registry_get "$thread" bot)"; [[ -n "$bot" ]] || die "등록부에 bot 없음: $thread"
enc="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$emoji")"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
for ch in "$thread" "$(registry_get "$thread" channelId)"; do
  [[ "$ch" == <-> ]] || continue
  # 서브셸 없이 호출해야 DISCORD_HTTP 를 읽을 수 있다
  if discord_api "$bot" PUT "/channels/$ch/messages/$mid/reactions/$enc/@me" > "$tmp"; then print -- "reacted"; exit 0; fi
  [[ "$DISCORD_HTTP" == 404 ]] && grep -q '"code": *10008' "$tmp" || break
done
die "react 실패 HTTP ${DISCORD_HTTP:-?}"

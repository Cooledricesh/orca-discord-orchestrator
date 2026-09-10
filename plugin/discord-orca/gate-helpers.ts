// orca fork: 순수 게이트 헬퍼 (테스트 가능하도록 server.ts 에서 분리).

/**
 * DISCORD_IGNORE_OTHER_BOT_MENTIONS=1 용.
 * 메시지가 "다른 봇 사용자"를 멘션하면서 이 클라이언트(selfId)는 멘션하지 않았으면 true (= drop).
 * 이 클라이언트를 함께 멘션했거나, 봇 멘션이 전혀 없으면 false.
 */
export function mentionsOtherBotOnly(
  mentioned: ReadonlyArray<{ id: string; bot: boolean }>,
  selfId: string | undefined,
): boolean {
  let other = false
  for (const u of mentioned) {
    if (selfId && u.id === selfId) return false
    if (u.bot) other = true
  }
  return other
}

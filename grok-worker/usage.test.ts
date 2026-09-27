import { expect, test } from 'bun:test'
import { postsResult, usageLine } from './usage.ts'
test('usage line matches lib.sh grok_usage_line format', () => {
  const j = JSON.stringify({ session: { inputTokens: 523412, cachedReadTokens: 373000, outputTokens: 15230, costUsdTicks: 1950940400, primaryModelId: 'grok-4.7-build', turnCount: 1 } })
  expect(usageLine(j)).toBe('토큰 입력 523k(캐시 373k) · 출력 15k · $0.195(환산) · grok-4.7-build · 1턴')
  expect(usageLine('not json')).toBeUndefined()
  expect(usageLine('{}')).toBeUndefined()
})
test('result card detection is limited to post-result.sh shell calls', () => {
  expect(postsResult('run_terminal_command', '`"$ORCH_ROOT/bin/post-result.sh" 1 succeeded "t" "b"`')).toBe(true)
  expect(postsResult('read_file', 'Read bin/post-result.sh')).toBe(false)
  expect(postsResult('run_terminal_command', '`git status`')).toBe(false)
})

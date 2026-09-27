/** `grok usage <sid>` JSON → one card line (same format as lib.sh grok_usage_line). Session totals are persisted only
 * after a turn ends, so the bridge appends this to the turn's result card instead of post-result.sh (which runs mid-turn). */
const k = (v: unknown) => { const n = Number(v) || 0; return n < 1000 ? String(n) : n < 10000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k` }
export function usageLine(json: string): string | undefined {
  let s: any
  try { s = JSON.parse(json)?.session } catch { return undefined }
  if (!s || typeof s !== 'object') return undefined
  const cost = (Number(s.costUsdTicks) || 0) / 1e10
  return `토큰 입력 ${k(s.inputTokens)}(캐시 ${k(s.cachedReadTokens)}) · 출력 ${k(s.outputTokens)} · $${cost.toFixed(3)}(환산) · ${s.primaryModelId || '?'} · ${Number(s.turnCount) || 0}턴`
}
/** The result card is posted from grok's shell as `…/bin/post-result.sh <tid> …`. */
export const postsResult = (name: string, desc: string) => name === 'run_terminal_command' && desc.includes('post-result.sh')

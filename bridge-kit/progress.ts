/** Progress card per turn, same format and cadence as bin/progress-hook.py: `⏳ N · <line>` posted on the first step,
 * edited with a 5s throttle, deleted at turn end. Bridge-only; the model never sees it. All Discord errors are swallowed. */
export interface ProgressIO {
  post(chat: string, content: string): Promise<string>
  edit(chat: string, messageId: string, content: string): Promise<void>
  remove(chat: string, messageId: string): Promise<void>
}
export type ProgressRef = { chat: string; messageId: string }
export const MAX_LINE = 120
export const progressBody = (n: number, line: string, suffix = '') => `⏳ ${n} · ${line.length > MAX_LINE ? line.slice(0, MAX_LINE - 1) + '…' : line}${suffix}`
type Card = { chat: string; messageId: string; n: number; line: string; lastEdit: number; pending?: string; timer?: ReturnType<typeof setTimeout>; posting?: Promise<void> }
export class ProgressCard {
  private card: Card | undefined
  private suffix = ''
  /** onChange: persist the live card (restart cleanup) — called with the ref after POST, undefined at end. */
  constructor(private io: ProgressIO, private opts: { throttleMs?: number; onChange?: (ref: ProgressRef | undefined) => void } = {}) {}
  get active() { return !!this.card }
  get count() { return this.card?.n ?? 0 }
  async step(chat: string, line: string) {
    if (!this.card) {
      const c: Card = this.card = { chat, messageId: '', n: 1, line, lastEdit: Date.now() }
      c.posting = (async () => {
        try { c.messageId = await this.io.post(chat, progressBody(1, line, this.suffix)); if (this.card === c) this.opts.onChange?.({ chat, messageId: c.messageId }) }
        catch { if (this.card === c) this.card = undefined }
      })()
      return c.posting
    }
    const c = this.card; c.n++; c.line = line; this.schedule(c)
  }
  /** Trailing note such as ` (+2 대기)`; re-renders the live card through the same throttle. */
  setSuffix(suffix: string) { if (suffix === this.suffix) return; this.suffix = suffix; if (this.card) this.schedule(this.card) }
  private schedule(c: Card) {
    c.pending = progressBody(c.n, c.line, this.suffix)
    if (c.timer) return
    const wait = Math.max(0, (this.opts.throttleMs ?? 5000) - (Date.now() - c.lastEdit))
    c.timer = setTimeout(async () => {
      c.timer = undefined; if (this.card !== c || !c.pending || !c.messageId) return
      const content = c.pending; c.pending = undefined; c.lastEdit = Date.now()
      try { await this.io.edit(c.chat, c.messageId, content) } catch {}
    }, wait)
  }
  async end() {
    const c = this.card; this.card = undefined; this.suffix = ''; this.opts.onChange?.(undefined)
    if (!c) return
    if (c.timer) clearTimeout(c.timer)
    await c.posting   // a card still being posted is deleted once it exists
    if (c.messageId) try { await this.io.remove(c.chat, c.messageId) } catch {}
  }
}
/** Discord typing lasts ~10s; refresh every 8s until stopped. */
export function keepTyping(send: () => Promise<unknown>, everyMs = 8000): () => void {
  const tick = () => { void send().catch(() => {}) }
  tick(); const timer = setInterval(tick, everyMs)
  return () => clearInterval(timer)
}

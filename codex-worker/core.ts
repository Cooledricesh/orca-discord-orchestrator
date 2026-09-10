// Codex app-server v2 shapes we use (from `codex app-server generate-ts`, 0.153.4)
type UserInput = { type: 'text'; text: string; text_elements: unknown[] }
type ThreadQueueAddParams = { threadId: string; input: UserInput[]; clientUserMessageId: string }
type TurnSteerParams = ThreadQueueAddParams & { expectedTurnId: string }
/** Transport-independent routing. JSON-RPC errors are definitive rejections; transport
 * errors are ambiguous and must never be retried as fresh turns or external effects. */
export class RpcError extends Error { constructor(public code: number, message: string, public data?: unknown) { super(message) } }
export interface RPC { call(method: string, params: any): Promise<any>; respond(id: string | number, result: any): void }
export type Envelope = { id?: string | number; method: string; params: any }
export type Ledger = Record<string, { state: 'claimed' | 'done'; result?: any }>
/** At-most-once execution keyed by a stable id, journaled so a restart never repeats an external send. */
export class Once {
  private running = new Map<string, Promise<any>>()
  constructor(public ledger: Ledger, private save: () => void) {}
  run(key: string, fn: () => Promise<any>): Promise<any> {
    const live = this.running.get(key); if (live) return live
    const previous = this.ledger[key]
    if (previous) return Promise.resolve(previous.state === 'done' ? previous.result : {
      success: false, contentItems: [{ type: 'inputText', text: 'Previous execution interrupted; delivery uncertain. Inspect history before repeating.' }],
    })
    this.ledger[key] = { state: 'claimed' }; this.save()
    const promise = Promise.resolve().then(fn).then(result => {
      this.ledger[key] = { state: 'done', result }; this.save(); return result
    }).finally(() => this.running.delete(key))
    this.running.set(key, promise); return promise
  }
}
/** Idle → turn/start, busy → turn/steer. Inputs are serialized per session. */
export class InputRouter {
  active: string | null = null
  private completed = new Set<string>()
  private serial: Promise<unknown> = Promise.resolve()
  constructor(readonly threadId: string, private rpc: RPC, private once: Once, private effort?: string) {}
  event(m: Envelope) {
    if (m.params?.threadId !== this.threadId) return
    const turn = m.params.turn
    if (m.method === 'turn/started' && !this.completed.has(turn.id)) this.active = turn.id
    if (m.method === 'turn/completed') {
      this.completed.add(turn.id)
      if (this.active === turn.id) this.active = null
    }
  }
  input(id: string, text: string): Promise<any> {
    const run = () => this.once.run(`input:${id}`, async () => {
      const params: ThreadQueueAddParams = { threadId: this.threadId, clientUserMessageId: id, input: [{ type: 'text', text, text_elements: [] }] }
      // A local state snapshot can race a CLI start or a completed turn. Refresh only
      // after explicit rejection; never replay on timeout/disconnection.
      for (let n = 0; n < 4; n++) {
        const expectedTurnId = this.active
        try {
          if (expectedTurnId) return await this.rpc.call('turn/steer', { ...params, expectedTurnId } satisfies TurnSteerParams)
          const r = await this.rpc.call('turn/start', { ...params, ...(this.effort ? { effort: this.effort } : {}) })
          if (!this.completed.has(r.turn.id)) this.active = r.turn.id
          return r
        } catch (e) {
          if (!expectedTurnId || !isTurnRace(e)) throw e
          const r = await this.rpc.call('thread/read', { threadId: this.threadId, includeTurns: true })
          this.active = [...(r.thread.turns ?? [])].reverse().find((t: any) => t.status === 'inProgress')?.id ?? null
        }
      }
      // Server-owned queue is safe only after definitive race rejections.
      return this.rpc.call('thread/queue/add', params)
    })
    const next = this.serial.then(run, run); this.serial = next.catch(() => {}); return next
  }
}
export function isTurnRace(e: unknown): boolean {
  // Exact steer rejections observed with native Codex 0.153.4. Unknown start,
  // review, policy and transport failures must never enter the retry/queue path.
  return e instanceof RpcError && e.code === -32600 && (
    e.message === 'no active turn to steer' ||
    /^expected active turn id `[^`\r\n]+` but found `[^`\r\n]+`$/.test(e.message)
  )
}

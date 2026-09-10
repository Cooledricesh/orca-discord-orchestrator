// 동시성: chat_id 별 FIFO 뮤텍스 + 전역 세마포어.
export class Semaphore {
  private active = 0
  private waiters: Array<() => void> = []
  constructor(public readonly max: number) {}
  async acquire(): Promise<() => void> {
    if (this.active < this.max) { this.active++; return () => this.release() }
    // 슬롯은 release() 가 넘겨준다 (active 는 그대로) — 그 사이 다른 acquire 가 끼어들어 max 를 넘기지 않게.
    await new Promise<void>(r => this.waiters.push(r))
    return () => this.release()
  }
  private release() { const w = this.waiters.shift(); if (w) w(); else this.active-- }
  get inUse() { return this.active }
  get queued() { return this.waiters.length }
}

export class PerKeyQueue {
  private tails = new Map<string, Promise<unknown>>()
  private depth = new Map<string, number>()
  /** 같은 key 의 이전 작업이 끝난 뒤 fn 실행. 반환된 Promise 는 fn 의 결과. */
  run<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const prev = this.tails.get(key) ?? Promise.resolve()
    this.depth.set(key, (this.depth.get(key) ?? 0) + 1)
    const p = prev.catch(() => {}).then(fn)
    const tail = p.catch(() => {}).finally(() => {
      const d = (this.depth.get(key) ?? 1) - 1
      if (d <= 0) { this.depth.delete(key); if (this.tails.get(key) === tail) this.tails.delete(key) } else this.depth.set(key, d)
    })
    this.tails.set(key, tail)
    return p
  }
  pending(key: string): number { return this.depth.get(key) ?? 0 }
}

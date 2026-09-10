import { RpcError, type RPC, type Envelope } from './core.ts'
export class SocketRPC implements RPC {
  private seq = 0
  private pending = new Map<string, { resolve: (x: any) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }>()
  onEvent: (m: Envelope) => void = () => {}
  onClose: () => void = () => {}
  constructor(readonly ws: WebSocket) {
    ws.onmessage = e => {
      const m = JSON.parse(String(e.data))
      if (m.method) { this.onEvent(m); return }
      const p = this.pending.get(String(m.id)); if (!p) return
      clearTimeout(p.timer); this.pending.delete(String(m.id))
      m.error ? p.reject(new RpcError(m.error.code, m.error.message, m.error.data)) : p.resolve(m.result)
    }
    ws.onclose = () => {
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(Error('App-server disconnected; request outcome uncertain')) }
      this.pending.clear(); this.onClose()
    }
  }
  static async connect(url: string) {
    const ws = await new Promise<WebSocket>((resolve, reject) => {
      const w = new WebSocket(url); const timer = setTimeout(() => { w.close(); reject(Error('Connect timeout')) }, 2000)
      w.onopen = () => { clearTimeout(timer); resolve(w) }; w.onerror = () => { clearTimeout(timer); reject(Error('Connect failed')) }
    })
    const rpc = new SocketRPC(ws)
    await rpc.call('initialize', { clientInfo: { name: 'orca_discord_worker', version: '1.0.0' }, capabilities: { experimentalApi: true } })
    ws.send(JSON.stringify({ method: 'initialized' })); return rpc
  }
  call(method: string, params: any): Promise<any> {
    const id = `bridge-${++this.seq}`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(Error(`App-server timeout (${method}); outcome uncertain`)) }, 30000)
      this.pending.set(id, { resolve, reject, timer }); this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  reject(id: string | number, code: number, message: string) { this.ws.send(JSON.stringify({ id, error: { code, message } })) }
  respond(id: string | number, result: any) { this.ws.send(JSON.stringify({ id, result })) }
}

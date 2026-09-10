import { test, expect } from 'bun:test'
import { InputRouter, Once, RpcError, isTurnRace, type RPC, type Ledger } from './core.ts'
import { PersistenceError } from './storage.ts'
const event = (method: string, tid: string, turn: string) => ({ method, params: { threadId: tid, turn: { id: turn } } })
function fake(fn: (method: string, params: any) => Promise<any> = async () => ({ turn: { id: 'a' } })) {
  const calls: any[] = []
  const rpc: RPC = { call: async (m, p) => { calls.push([m, p]); return fn(m, p) }, respond: () => {} }
  return { calls, rpc }
}
test('idle starts, active steers; duplicate input processed once', async () => {
  const f = fake(); const r = new InputRouter('root', f.rpc, new Once({}, () => {}))
  await Promise.all([r.input('1', 'hello'), r.input('1', 'hello'), r.input('2', 'more')])
  expect(f.calls.map(x => x[0])).toEqual(['turn/start', 'turn/steer'])
  expect(f.calls[1][1].expectedTurnId).toBe('a')
})
test('completed-before-start-response and child events never overwrite root active turn', async () => {
  let r: InputRouter
  const f = fake(async () => { r.event(event('turn/completed', 'root', 'a')); return { turn: { id: 'a' } } })
  r = new InputRouter('root', f.rpc, new Once({}, () => {})); await r.input('1', 'x'); expect(r.active).toBeNull()
  r.event(event('turn/started', 'root', 'b')); r.event(event('turn/started', 'child', 'c')); r.event(event('turn/completed', 'child', 'b'))
  expect(r.active).toBe('b'); r.event(event('turn/completed', 'root', 'a')); expect(r.active).toBe('b')
})
test('steer completion race refreshes and starts with same input identity', async () => {
  const f = fake(async m => { if (m === 'turn/steer') throw new RpcError(-32600, 'no active turn to steer'); if (m === 'thread/read') return { thread: { turns: [] } }; return { turn: { id: 'new' } } })
  const r = new InputRouter('root', f.rpc, new Once({}, () => {})); r.active = 'old'; await r.input('m1', 'x')
  expect(f.calls.map(x => x[0])).toEqual(['turn/steer', 'thread/read', 'turn/start'])
  expect(f.calls[2][1].clientUserMessageId).toBe('m1')
})
test('known steer mismatch refreshes live turn; repeated definite races enqueue', async () => {
  const f = fake(async m => { if (m === 'thread/read') return { thread: { turns: [{ id: 'cli', status: 'inProgress' }] } }; if (m === 'thread/queue/add') return {}; throw new RpcError(-32600, 'expected active turn id `old` but found `cli`') })
  const r = new InputRouter('root', f.rpc, new Once({}, () => {})); r.active = 'old'; await r.input('id', 'hello')
  expect(f.calls.at(-1)).toEqual(['thread/queue/add', { threadId: 'root', clientUserMessageId: 'id', input: [{ type: 'text', text: 'hello', text_elements: [] }] }])
})
test('ambiguous timeout never retries and survives restart as uncertain', async () => {
  const ledger: Ledger = {}; const f = fake(async () => { throw Error('timeout') })
  const r = new InputRouter('r', f.rpc, new Once(ledger, () => {}))
  await expect(r.input('1', 'x')).rejects.toThrow('timeout')
  const r2 = new InputRouter('r', f.rpc, new Once(ledger, () => {})); const result = await r2.input('1', 'x')
  expect(result.success).toBe(false); expect(f.calls).toHaveLength(1)
})
test('dynamic tool in-flight duplicates and replay execute exactly once', async () => {
  let n = 0; const ledger: Ledger = {}; const once = new Once(ledger, () => {})
  const fn = async () => { n++; await Bun.sleep(5); return { success: true } }
  await Promise.all([once.run('tool:r:c', fn), once.run('tool:r:c', fn)])
  await new Once(ledger, () => {}).run('tool:r:c', fn); expect(n).toBe(1)
})
test('ledger persistence failure propagates', () => {
  const once = new Once({}, () => { throw new PersistenceError('disk') })
  expect(() => once.run('input', async () => ({}))).toThrow(PersistenceError)
})
test('race classification rejects lookalike errors with unrelated codes', () => {
  expect(isTurnRace(new RpcError(-32602, 'no active turn to steer'))).toBe(false)
  expect(isTurnRace(new RpcError(-32000, 'expected active turn id `old` but found `cli`'))).toBe(false)
  expect(isTurnRace(new RpcError(-32600, 'review turn cannot accept input'))).toBe(false)
})
test('unconfirmed turn-start rejection is never refreshed or enqueued', async () => {
  const f = fake(async () => { throw new RpcError(-32600, 'active turn already in progress') })
  const r = new InputRouter('root', f.rpc, new Once({}, () => {}))
  await expect(r.input('id', 'hello')).rejects.toThrow('active turn already in progress')
  expect(f.calls.map(x => x[0])).toEqual(['turn/start'])
})

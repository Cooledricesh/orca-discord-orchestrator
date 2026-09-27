import { test, expect } from 'bun:test'
import { ProgressCard, progressBody, keepTyping } from '../bridge-kit/progress.ts'
function io() {
  const log: string[] = []
  return { log, io: { post: async (c: string, t: string) => { log.push(`post ${c} ${t}`); return 'm1' }, edit: async (c: string, id: string, t: string) => { log.push(`edit ${id} ${t}`) }, remove: async (c: string, id: string) => { log.push(`delete ${id}`) } } }
}
test('first step posts, later steps edit through the throttle, end deletes', async () => {
  const { log, io: x } = io(), refs: any[] = []
  const card = new ProgressCard(x, { throttleMs: 50, onChange: r => refs.push(r) })
  await card.step('t', 'Read a'); await card.step('t', 'Edit b'); await card.step('t', 'Edit c')
  card.setSuffix(' (+1 대기)'); await Bun.sleep(80); await card.end()
  expect(log).toEqual(['post t ⏳ 1 · Read a', 'edit m1 ⏳ 3 · Edit c (+1 대기)', 'delete m1'])
  expect(refs).toEqual([{ chat: 't', messageId: 'm1' }, undefined])
})
test('end during an in-flight post still deletes the card; failed post drops it', async () => {
  const log: string[] = []; let release!: () => void
  const card = new ProgressCard({ post: () => new Promise(r => { release = () => r('m2') }), edit: async () => {}, remove: async (_c, id) => { log.push(id) } })
  const step = card.step('t', 'x'); const end = card.end(); release(); await step; await end
  expect(log).toEqual(['m2'])
  const bad = new ProgressCard({ post: async () => { throw Error('403') }, edit: async () => {}, remove: async () => {} })
  await bad.step('t', 'x'); expect(bad.active).toBe(false)
})
test('lines are capped at 120 chars', () => {
  expect(progressBody(2, 'x'.repeat(200))).toBe('⏳ 2 · ' + 'x'.repeat(119) + '…')
  expect(progressBody(1, 'short', ' (+2 대기)')).toBe('⏳ 1 · short (+2 대기)')
})
test('typing refreshes until stopped', async () => {
  let n = 0; const stop = keepTyping(async () => { n++ }, 20); await Bun.sleep(70); stop(); const seen = n; await Bun.sleep(50)
  expect(seen).toBeGreaterThanOrEqual(3); expect(n).toBe(seen)
})

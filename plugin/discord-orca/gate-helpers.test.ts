import { describe, expect, test } from 'bun:test'
import { mentionsOtherBotOnly } from './gate-helpers.ts'

const ME = 'self-1'
describe('mentionsOtherBotOnly', () => {
  test('no mentions → keep', () => expect(mentionsOtherBotOnly([], ME)).toBe(false))
  test('only humans → keep', () => expect(mentionsOtherBotOnly([{ id: 'u1', bot: false }], ME)).toBe(false))
  test('other bot only → drop', () => expect(mentionsOtherBotOnly([{ id: 'jarvis', bot: true }], ME)).toBe(true))
  test('other bot + self → keep', () => expect(mentionsOtherBotOnly([{ id: 'jarvis', bot: true }, { id: ME, bot: true }], ME)).toBe(false))
  test('self only → keep', () => expect(mentionsOtherBotOnly([{ id: ME, bot: true }], ME)).toBe(false))
  test('unknown self id, other bot → drop', () => expect(mentionsOtherBotOnly([{ id: 'jarvis', bot: true }], undefined)).toBe(true))
})

// This adapter has startup side effects; check its emitted metadata contract without
// importing it and opening a live Gateway connection.
test('Claude channel notification marks bot authors explicitly', async () => {
  const source = await Bun.file(new URL('./server.ts', import.meta.url)).text()
  const notification = source.slice(source.indexOf("method: 'notifications/claude/channel'"))
  expect(notification).toContain('author_is_bot: String(msg.author.bot)')
})

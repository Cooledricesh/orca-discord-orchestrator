import { test, expect, afterEach } from 'bun:test'
import { DiscordPort } from './discord.ts'
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
const roots: string[] = []
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }) })
function port() {
  const stateDir = mkdtempSync(join(tmpdir(), 'vision-')); roots.push(stateDir)
  const d = new DiscordPort({ channel: 'lounge', guild: 'guild', owner: 'owner', stateDir })
  Object.defineProperty(d.client, 'user', { value: { id: 'self' } }); return d
}
function msg(author: string, bot: boolean, mentions: any[] = [], channelId = 'lounge'): any {
  return { guildId: 'guild', channelId, webhookId: null, author: { id: author, bot }, content: 'hello', mentions: { parsedUsers: new Map(mentions.map(m => [m.id, m])) } }
}
test('owner messages in the lounge and its threads are accepted; bots, webhooks, outsiders and other channels are not', () => {
  const d = port()
  expect(d.accepts(msg('owner', false))).toBe(true)
  expect(d.accepts({ ...msg('owner', false, [], 'child'), channel: { isThread: () => true, parentId: 'lounge' } })).toBe(true)
  expect(d.accepts({ ...msg('owner', false, [], 'child'), channel: { isThread: () => true, parentId: 'other' } })).toBe(false)
  expect(d.accepts(msg('owner', false, [], 'other'))).toBe(false)
  expect(d.accepts(msg('outsider', false))).toBe(false)
  expect(d.accepts(msg('bot', true))).toBe(false)
  expect(d.accepts({ ...msg('owner', false), webhookId: 'w' })).toBe(false)
  expect(d.accepts({ ...msg('owner', false), guildId: 'other' })).toBe(false)
  d.client.destroy()
})
test('a message that mentions only another bot is left to that bot', () => {
  const d = port()
  expect(d.accepts(msg('owner', false, [{ id: 'jarvis', bot: true }]))).toBe(false)
  expect(d.accepts(msg('owner', false, [{ id: 'jarvis', bot: true }, { id: 'self', bot: true }]))).toBe(true)
  d.client.destroy()
})
test('tools require chat_id inside the lounge; result threads only from the lounge channel', async () => {
  const d = port(); const sent: any[] = []
  d.client.channels.fetch = (async (id: string) => ({ id, guildId: 'guild', parentId: id === 'child' ? 'lounge' : 'other', isThread: () => id !== 'lounge', messages: {},
    send: async (args: any) => { sent.push({ id, ...args }); return { id: 'reply' } }, threads: { create: async (args: any) => ({ id: 'new-child', ...args }) } })) as any
  await expect(d.tool('discord_reply', { text: 'hello' })).rejects.toThrow('chat_id')
  await d.tool('discord_reply', { text: 'hello', chat_id: 'child' }); expect(sent[0].id).toBe('child')
  await expect(d.tool('discord_reply', { text: 'hello', chat_id: 'unrelated' })).rejects.toThrow('lounge')
  expect(JSON.parse(await d.tool('discord_create_thread', { chat_id: 'lounge', name: 'Result' })).chat_id).toBe('new-child')
  await expect(d.tool('discord_create_thread', { chat_id: 'child', name: 'Result' })).rejects.toThrow()
  d.client.destroy()
})
test('attachments from private roots or through symlinks are refused', () => {
  const d = port(), privateRoot = mkdtempSync(join(tmpdir(), 'vision-private-')), inbox = join(d.cfg.stateDir, 'inbox')
  roots.push(privateRoot); mkdirSync(inbox)
  const secret = join(privateRoot, 'runtime.json'); writeFileSync(secret, 'x')
  const link = join(inbox, 'report.json'); symlinkSync(secret, link)
  d.cfg.privateRoots = [privateRoot]
  expect(() => d.assertFile(secret)).toThrow(); expect(() => d.assertFile(link)).toThrow()
  const ok = join(inbox, 'report.txt'); writeFileSync(ok, 'safe'); expect(() => d.assertFile(ok)).not.toThrow()
  d.client.destroy()
})

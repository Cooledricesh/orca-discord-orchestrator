import { afterEach, beforeEach, expect, spyOn, test } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { ChannelType } from 'discord.js'
import { handleModelCommand, isModelCommand } from './model-control.ts'

let dir: string
let previous: NodeJS.ProcessEnv
beforeEach(() => {
  previous = { ...process.env }
  dir = mkdtempSync(join(tmpdir(), 'model-control-test-'))
  process.env.ORCA_ROLE = '상담역'; process.env.ORCH_ROOT = dir
  process.env.ROUTES_FILE = join(dir, 'routes.json')
  writeFileSync(process.env.ROUTES_FILE, JSON.stringify({ ownerUserId: 'owner' }))
})
afterEach(() => { process.env = previous; rmSync(dir, { recursive: true, force: true }) })
function message(content: string) {
  const replies: any[] = []
  return { content, author: { id: 'owner', bot: false }, channel: { type: ChannelType.DM }, channelId: '123',
    reply: async (payload: any) => { replies.push(payload); return {} }, replies }
}
test('explicit prefix only; ordinary model conversation is not intercepted', () => {
  expect(isModelCommand('!모델 해피 sonnet')).toBe(true)
  expect(isModelCommand('!model')).toBe(true)
  expect(isModelCommand('모델을 바꿀까?')).toBe(false)
  expect(isModelCommand('!모델이뭐야')).toBe(false)
})
test('ordinary message and other role pass through', async () => {
  expect(await handleModelCommand(message('안녕') as any)).toBe(false)
  process.env.ORCA_ROLE = '접수원'
  expect(await handleModelCommand(message('!모델') as any)).toBe(false)
})
test('other author and bot cannot issue commands', async () => {
  const msg = message('!모델'); msg.author.id = 'stranger'
  expect(await handleModelCommand(msg as any)).toBe(true); expect(msg.replies).toHaveLength(0)
  msg.author.id = 'owner'; msg.author.bot = true
  expect(await handleModelCommand(msg as any)).toBe(true); expect(msg.replies).toHaveLength(0)
})
test('server channel responds with DM guidance without invoking model', async () => {
  const msg = message('!모델'); msg.channel.type = ChannelType.GuildText as any
  expect(await handleModelCommand(msg as any)).toBe(true)
  expect(msg.replies[0].content).toContain('DM')
})
test('owner query uses local process and is consumed before Claude', async () => {
  const msg = message('!모델')
  let envelope = ''
  const fake = spyOn(Bun, 'spawn').mockReturnValue({ stdin: { write: (s: string) => { envelope = s }, end() {} },
    stdout: new Blob([JSON.stringify({ text: '설정 모델 목록' })]).stream(), exited: Promise.resolve(0) } as any)
  try {
    expect(await handleModelCommand(msg as any)).toBe(true)
    expect(JSON.parse(envelope).authorId).toBe('owner')
    expect(msg.replies[0].content).toBe('설정 모델 목록')
    expect(msg.replies[0].allowedMentions.parse).toEqual([])
  } finally { fake.mockRestore() }
})
test('handler errors never fall through to Claude', async () => {
  const msg = message('!모델')
  const fake = spyOn(Bun, 'spawn').mockImplementation(() => { throw new Error('failure') })
  try {
    expect(await handleModelCommand(msg as any)).toBe(true)
    expect(msg.replies[0].content).toContain('Claude에 전달하지 않았습니다')
  } finally { fake.mockRestore() }
})

import { describe, expect, test } from 'bun:test'
import { gate, stripSelfMention, RateGuard, type GateInput, type GateCfg } from './gate.ts'
import { formatHistory, buildPrompt, resolveTarget, routeReadPath, discordMessageText } from './context.ts'
import { GENERAL_CWD, type Routes } from './config.ts'
import { extractSummary } from './post.ts'
import { PerKeyQueue, Semaphore } from './queue.ts'

const OWNER = 'owner', GUILD = 'g1', SELF = 'jarvis', CH = 'ch-proj', GEN = 'ch-general'
const processed = new Set<string>()
const cfg: GateCfg = {
  ownerUserId: OWNER, guildId: GUILD, allowedChannelIds: new Set([CH, GEN]), selfAppId: SELF,
  isProcessed: (c, m) => processed.has(`${c}:${m}`),
}
const base: GateInput = {
  messageId: 'm1', authorId: OWNER, authorIsBot: false, guildId: GUILD, channelId: 'thread-1', parentId: CH, mentionedUserIds: [SELF],
}

describe('gate', () => {
  test('owner mention in routed thread → deliver', () => {
    expect(gate(base, cfg)).toEqual({ action: 'deliver', chatId: 'thread-1', parentChannelId: CH })
  })
  test('channel-level message → deliver with chatId = channel', () => {
    expect(gate({ ...base, channelId: GEN, parentId: null }, cfg)).toEqual({ action: 'deliver', chatId: GEN, parentChannelId: GEN })
  })
  test('non-owner → drop', () => expect(gate({ ...base, authorId: 'someone' }, cfg).action).toBe('drop'))
  test('bot author (even owner id) → drop', () => expect(gate({ ...base, authorIsBot: true }, cfg).action).toBe('drop'))
  test('other guild → drop', () => expect(gate({ ...base, guildId: 'g2' }, cfg).action).toBe('drop'))
  test('unrouted channel → drop', () => expect(gate({ ...base, channelId: 'x', parentId: 'ch-other' }, cfg).action).toBe('drop'))
  test('no mention → drop', () => expect(gate({ ...base, mentionedUserIds: [] }, cfg).action).toBe('drop'))
  test('mentions another bot only → drop', () => expect(gate({ ...base, mentionedUserIds: ['mark1'] }, cfg).action).toBe('drop'))
  test('mentions another bot and self → deliver', () => expect(gate({ ...base, mentionedUserIds: ['mark1', SELF] }, cfg).action).toBe('deliver'))
  test('already processed → drop', () => {
    processed.add('thread-1:m1')
    expect(gate(base, cfg)).toEqual({ action: 'drop', reason: 'already processed' })
    processed.clear()
  })
})

describe('gate: trusted bots', () => {
  test('trusted bot brief → deliver', () => expect(gate({ ...base, authorId: 'friday', authorIsBot: true, authorIsTrustedBot: true }, cfg).action).toBe('deliver'))
  test('untrusted bot (해피) → drop', () => expect(gate({ ...base, authorId: 'happy', authorIsBot: true, authorIsTrustedBot: false }, cfg)).toEqual({ action: 'drop', reason: 'untrusted bot author' }))
  test('owner id claimed by a bot account → drop', () => expect(gate({ ...base, authorIsBot: true }, cfg).action).toBe('drop'))
  test('rate guard: 4th bot request in 60s dropped, owner unaffected', () => {
    let t = 0; const rg = new RateGuard(3, 60_000, 60_000, () => t)
    const c2 = { ...cfg, rateGuard: rg }
    const bot = { ...base, authorId: 'mark1', authorIsBot: true, authorIsTrustedBot: true }
    for (let i = 0; i < 3; i++) expect(gate({ ...bot, messageId: `b${i}` }, c2).action).toBe('deliver')
    expect(gate({ ...bot, messageId: 'b3' }, c2)).toEqual({ action: 'drop', reason: 'bot rate limit' })
    expect(gate({ ...base, messageId: 'o1' }, c2).action).toBe('deliver')
    t = 30_000; expect(gate({ ...bot, messageId: 'b4' }, c2).action).toBe('drop')
    t = 61_000; expect(gate({ ...bot, messageId: 'b5' }, c2).action).toBe('deliver')
    expect(gate({ ...bot, messageId: 'b6', channelId: 'thread-2' }, c2).action).toBe('deliver')
  })
})

describe('gate: owner DM', () => {
  const dm = { ...base, guildId: null, isDirectMessage: true, channelId: 'dm-owner', parentId: null, mentionedUserIds: [] }
  test('owner DM needs neither route nor mention', () => {
    expect(gate(dm, cfg)).toEqual({ action: 'deliver', chatId: 'dm-owner', parentChannelId: 'dm-owner', isDirectMessage: true })
  })
  test('non-owner DM rejected even with mention', () => {
    expect(gate({ ...dm, authorId: 'stranger', mentionedUserIds: [SELF] }, cfg).action).toBe('drop')
  })
  test('trusted bot DM rejected', () => {
    expect(gate({ ...dm, authorId: 'friday', authorIsBot: true, authorIsTrustedBot: true }, cfg)).toEqual({ action: 'drop', reason: 'DM owner only' })
  })
  test('group DM / unidentified guild-less message rejected', () => {
    expect(gate({ ...dm, isDirectMessage: false }, cfg).action).toBe('drop')
  })
  test('DM flag does not bypass foreign guild restriction', () => {
    expect(gate({ ...dm, guildId: 'other' }, cfg).action).toBe('drop')
  })
  test('processed owner DM is not replayed', () => {
    expect(gate(dm, { ...cfg, isProcessed: () => true })).toEqual({ action: 'drop', reason: 'already processed' })
  })
})

describe('buildPrompt requester line', () => {
  test('bot requester adds 요청자 line; owner does not', () => {
    const tgt = { project: 'p', cwd: '/x', route: null, registry: null }
    const withBot = buildPrompt('브리프', { target: tgt, history: '', git: '', isFirstTurn: true, channelLevel: false, requesterBot: '마크 1' })
    expect(withBot.startsWith('## 요청\n요청자: 마크 1 (봇 브리프 — 항목별로 판정)\n브리프')).toBe(true)
    const owner = buildPrompt('질문', { target: tgt, history: '', git: '', isFirstTurn: true, channelLevel: false })
    expect(owner).not.toContain('요청자:')
  })
})

describe('stripSelfMention', () => {
  test('removes <@id> and <@!id>', () => expect(stripSelfMention('<@jarvis> 봐줘 <@!jarvis> 끝', 'jarvis')).toBe('봐줘 끝'))
  test('keeps other mentions', () => expect(stripSelfMention('<@jarvis> <@mark1> 봐줘', 'jarvis')).toBe('<@mark1> 봐줘'))
})

describe('context', () => {
  test('review history includes result embed title, body and fields', () => {
    expect(discordMessageText({ content: '<@owner>', embeds: [{ title: '완료', description: '세 줄 요약', fields: [{ name: '수정 파일', value: '없음' }] }], attachments: { size: 1 } }))
      .toBe('<@owner>\n[카드]\n완료\n세 줄 요약\n수정 파일: 없음\n[첨부 1개]')
  })
  test('plain and empty messages work without embeds', () => {
    expect(discordMessageText({ content: '검수해 줘' })).toBe('검수해 줘')
    expect(discordMessageText({ embeds: [{}] })).toBe('')
  })
  test('history caps by chars keeping the newest', () => {
    const msgs = Array.from({ length: 50 }, (_, i) => ({ id: String(i), author: 'u', isBot: false, content: `msg-${i} ` + 'x'.repeat(50), createdAt: '2026-09-08T00:00:00.000Z' }))
    const t = formatHistory(msgs, 500)
    expect(t.startsWith('… (앞부분 생략)')).toBe(true)
    expect(t).toContain('msg-49')
    expect(t).not.toContain('msg-0 ')
  })
  test('prompt puts the request first and delta after', () => {
    const p = buildPrompt('검토해', { target: { project: 'p', cwd: '/x', route: null, registry: null }, history: 'H', git: 'G', isFirstTurn: false, channelLevel: false })
    expect(p.indexOf('## 요청')).toBe(0)
    expect(p.indexOf('## 컨텍스트 갱신')).toBeGreaterThan(p.indexOf('## 요청'))
    expect(p.indexOf('## git')).toBeGreaterThan(p.indexOf('## 컨텍스트 갱신'))
  })
})

describe('resolveTarget', () => {
  const routes = { routes: { 'ch-proj': { name: 'p', path: '/nonexistent/p' } }, generalChannelId: 'ch-general', guildId: 'g', ownerUserId: 'o', bots: {} } as unknown as Routes
  test('DM uses neutral cwd, no implicit project and private DM instructions', () => {
    const t = resolveTarget(routes, 'dm-owner', 'dm-owner', true)
    expect(t).toEqual({ project: 'dm', cwd: GENERAL_CWD, route: null, registry: null, general: true, directMessage: true })
    const prompt = buildPrompt('안녕', { target: t, history: '', git: '', isFirstTurn: true, channelLevel: true, knownRoutes: Object.values(routes.routes) })
    expect(prompt).toContain('비공개 DM')
    expect(prompt).toContain('멘션 없이')
    expect(prompt).not.toContain('채널 최상위')
    expect(prompt).not.toContain('## git')
  })
  test('#프라이데이 thread → general target, empty cwd dir, no error', () => {
    const t = resolveTarget(routes, 'thread-g', 'ch-general')
    expect(t.general).toBe(true); expect(t.project).toBe('general'); expect(t.cwd).toBe(GENERAL_CWD); expect(t.error).toBeUndefined()
  })
  test('routed channel with missing path → error', () => {
    expect(resolveTarget(routes, 'thread-x', 'ch-proj').error).toContain('없습니다')
  })
  test('unrouted channel → error', () => {
    expect(resolveTarget(routes, 'thread-y', 'ch-other').error).toContain('routes.json')
  })
  test('general cwd lives outside any git repo (~/.jarvis)', () => {
    expect(GENERAL_CWD.includes('/.jarvis/')).toBe(true); expect(GENERAL_CWD.includes('/orchestrator/')).toBe(false)
  })
  test('routeReadPath uses writeDir when present', () => {
    expect(routeReadPath({ name: 'document', path: '/home/user/orchestrator', writeDir: 'docs/' })).toBe('/home/user/orchestrator/docs')
    expect(routeReadPath({ name: 'p', path: '/x/p' })).toBe('/x/p')
    const t = resolveTarget(routes, 'thread-g', 'ch-general')
    const p = buildPrompt('q', { target: t, history: '', git: '', isFirstTurn: true, channelLevel: false, knownRoutes: [{ name: 'document', path: '/home/user/orchestrator', writeDir: 'docs/' }] })
    expect(p).toContain('- document: /home/user/orchestrator/docs'); expect(p).not.toMatch(/- document: \/home\/user\/orchestrator\n/)
  })
  test('general prompt lists known projects and no git', () => {
    const t = resolveTarget(routes, 'thread-g', 'ch-general')
    const p = buildPrompt('의견 줘', { target: t, history: 'H', git: '', isFirstTurn: true, channelLevel: false, knownRoutes: Object.values(routes.routes) })
    expect(p).toContain('코드 대상 없음'); expect(p).toContain('- p: /nonexistent/p'); expect(p).not.toContain('## git')
  })
})

describe('post', () => {
  test('extractSummary prefers the 요약 section', () => {
    const s = extractSummary('판정: pass — ok\n\n## 요약\n한 줄 요약\n\n## 발견\n- 없음', 100)
    expect(s).toBe('한 줄 요약')
  })
})

describe('queue', () => {
  test('same key runs in order, different keys in parallel', async () => {
    const q = new PerKeyQueue(); const sem = new Semaphore(4); const order: string[] = []
    const job = (k: string, n: string, ms: number) => q.run(k, async () => { const r = await sem.acquire(); order.push(`${n}:start`); await Bun.sleep(ms); order.push(`${n}:end`); r() })
    await Promise.all([job('A', 'a1', 40), job('A', 'a2', 10), job('B', 'b1', 10)])
    expect(order.indexOf('a2:start')).toBeGreaterThan(order.indexOf('a1:end'))
    expect(order.indexOf('b1:end')).toBeLessThan(order.indexOf('a1:end'))
  })
  test('semaphore caps concurrency', async () => {
    const sem = new Semaphore(2); let active = 0, peak = 0
    await Promise.all(Array.from({ length: 6 }, async () => { const r = await sem.acquire(); active++; peak = Math.max(peak, active); await Bun.sleep(5); active--; r() }))
    expect(peak).toBe(2)
  })
})

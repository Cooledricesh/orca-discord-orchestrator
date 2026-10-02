#!/usr/bin/env bun
// 자비스 데몬. Discord 에서 소유자가 @자비스 를 멘션하면 Codex(읽기 전용)로 검토해 답한다.
//   bun server.ts                      Discord 연결 (pidfile state/jarvis.pid)
//   bun server.ts --dry-run [--fixture f.json | --message "text"] [--cwd dir] [--chat id]
//   bun server.ts cleanup <chatId>|--all      상태+codex 세션 파일 삭제
//   bun server.ts gc [--days N]             lastTurnAt 이 N일(기본 review.gcDays=30) 넘은 스레드 정리
//   bun server.ts reauth                    auth.json 을 설정된 원본 인증 파일로 강제 재링크
import { existsSync, readFileSync, writeFileSync, unlinkSync } from 'fs'
import { basename } from 'path'
import {
  loadConfig, loadBotEnv, loadTrustedBots, roleEnabled, ensureDirs, authSource, PID_FILE, type Config,
} from './config.ts'
import { gate, stripSelfMention, RateGuard, type GateInput } from './gate.ts'
import {
  readState, writeState, newState, markProcessed, isProcessed, listStates, deleteState, log, applyModelRevision, type ThreadState,
} from './state.ts'
import { resolveTarget, gitBlock, formatHistory, buildPrompt, discordMessageText, type HistMsg, type Target } from './context.ts'
import {
  prepareCodexHome, runTurn, recoverFinalMessage, deleteSessionFile, killAllChildren, inflightChildren, ensureAuthLink, isAuthError, authFailureNotice, type TurnResult,
} from './codex.ts'
import { Semaphore, PerKeyQueue } from './queue.ts'
import { buildFinal, ProgressReporter, summarizeProgress, ConsolePoster, DiscordPoster, type Poster } from './post.ts'

// ---------------------------------------------------------------------------
// 공통 파이프라인
// ---------------------------------------------------------------------------
interface Request {
  chatId: string
  isDirectMessage?: boolean
  parentChannelId: string
  messageId: string
  userText: string
  /** 이력 (first: 전체, after: lastMessageId 이후). 요청 메시지 자체는 제외. */
  fetchHistory: (after: string | null) => Promise<HistMsg[]>
  poster: Poster
  targetOverride?: Partial<Target>
  /** 요청자가 신뢰 봇이면 그 표시 이름 */
  requesterBot?: string
}

let shuttingDown = false

class Jarvis {
  readonly sem: Semaphore
  readonly q = new PerKeyQueue()
  readonly rateGuard = new RateGuard(3, 60_000, 60_000)
  /** 신뢰 봇 appId → 봇 이름 (프라이데이 + 마크). 해피·자비스·외부 봇은 없다. */
  trustedBots = new Map<string, string>()
  /** #운영-로그 게시 (모드가 주입). 실패해도 throw 하지 않는다. */
  opsLog: (text: string) => Promise<void> = async () => {}
  /** 'kept-newer' 경고는 프로세스당 한 번만 */
  private keptNewerWarned = false
  constructor(readonly cfg: Config, readonly roleVersion: string, readonly selfAppId: string) {
    this.sem = new Semaphore(cfg.review.maxConcurrent)
  }
  botDisplay(name: string): string { return this.cfg.routes.botDisplay?.[name] ?? name }

  gateCfg() {
    const r = this.cfg.routes
    return {
      ownerUserId: r.ownerUserId,
      guildId: r.guildId,
      allowedChannelIds: new Set([...Object.keys(r.routes), r.generalChannelId]),
      selfAppId: this.selfAppId,
      isProcessed,
      rateGuard: this.rateGuard,
    }
  }

  /** 요청 접수: 같은 chat 은 순서대로, 전체는 세마포어. */
  enqueue(req: Request): Promise<void> {
    const waiting = this.q.pending(req.chatId)
    if (waiting > 0) log(`[${req.chatId}] queued behind ${waiting} (msg ${req.messageId})`)
    return this.q.run(req.chatId, async () => {
      if (this.sem.inUse >= this.sem.max) log(`[${req.chatId}] waiting for slot (${this.sem.inUse}/${this.sem.max} busy, ${this.sem.queued} queued)`)
      const release = await this.sem.acquire()
      try { await this.handle(req) } finally { release() }
    })
  }

  private async handle(req: Request): Promise<void> {
    const { cfg } = this
    const target: Target = { ...resolveTarget(cfg.routes, req.chatId, req.parentChannelId, req.isDirectMessage), ...req.targetOverride }
    if (req.targetOverride?.cwd) delete target.error
    if (target.error) {
      log(`[${req.chatId}] target error: ${target.error}`)
      await req.poster.postFinal(null, { content: `❌ ${target.error}` })
      return
    }
    let st = readState(req.chatId)
    if (!st) st = newState(req.chatId, target.project, target.cwd, this.roleVersion)
    const modelRevision = (cfg.routes.models?.['리뷰어'] as { revision?: string } | undefined)?.revision ?? ''
    if (applyModelRevision(st, modelRevision)) {
      log(`[${req.chatId}] model configuration changed — new review context (old transcript kept)`)
    }
    if (st.cwd !== target.cwd) { log(`[${req.chatId}] cwd changed ${st.cwd} → ${target.cwd} (registry/route)`); st.cwd = target.cwd }
    // 채널 최상위 대화는 영구 resume 하지 않는다: 마지막 턴이 TTL 을 넘었으면 새 codex 스레드.
    if (req.chatId === req.parentChannelId && st.codexThreadId && st.lastTurnAt) {
      const ageH = (Date.now() - Date.parse(st.lastTurnAt)) / 3_600_000
      if (ageH > cfg.review.channelThreadTtlHours) {
        log(`[${req.chatId}] channel-level thread ${st.codexThreadId} is ${ageH.toFixed(1)}h old (> ${cfg.review.channelThreadTtlHours}h) → new codex thread`)
        deleteSessionFile(st.codexThreadId)
        st.codexThreadId = null; st.lastMessageId = null; st.roleVersion = this.roleVersion
      }
    }
    const isFirst = !st.codexThreadId
    const turn = st.turns + 1

    const history = await req.fetchHistory(isFirst ? null : st.lastMessageId).catch(e => { log(`[${req.chatId}] history fetch failed: ${e}`); return [] as HistMsg[] })
    const capped = history.slice(-cfg.review.historyMaxMessages)
    const histText = formatHistory(capped, cfg.review.historyMaxChars)
    const git = target.general ? '' : await gitBlock(target.cwd, target.registry?.baseRef)
    const prompt = buildPrompt(req.userText, {
      target, history: histText, git, isFirstTurn: isFirst, channelLevel: req.chatId === req.parentChannelId,
      knownRoutes: target.general ? Object.values(cfg.routes.routes) : undefined,
      requesterBot: req.requesterBot,
    })

    const progress = new ProgressReporter(req.poster, '⏳ 자비스')
    const progressId = await progress.start().catch(() => null)
    st.inflight = { messageId: req.messageId, startedAt: new Date().toISOString(), progressMessageId: progressId ?? undefined }
    markProcessed(st, req.messageId)
    writeState(st)

    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), cfg.review.timeoutMin * 60_000)
    const t0 = Date.now()
    if (process.env.JARVIS_SHOW_PROMPT) process.stdout.write(`\n=== [prompt] ===\n${prompt}\n`)
    const sections = prompt.split('\n').filter(l => l.startsWith('## ')).join(', ')
    log(`[${req.chatId}] turn ${turn} start (${isFirst ? 'new' : 'resume ' + st.codexThreadId}) cwd=${target.cwd} prompt=${prompt.length}c hist=${capped.length} sections=[${sections}]`)
    let res: TurnResult
    try {
      // 매 턴 전 auth.json 링크 재확인. 원본이 없으면 codex 를 돌리지 않고 실패 결과로 같은 경로를 탄다.
      let authErr: string | null = null
      try {
        if (ensureAuthLink(cfg) === 'kept-newer' && !this.keptNewerWarned) {
          this.keptNewerWarned = true
          void this.opsLog('⚠️ 자비스 Codex 인증 파일이 원본보다 새것이라 링크하지 않고 유지 중 — 원본 계정 로그인 상태를 확인한 뒤 `bun jarvis/server.ts reauth`').catch(() => {})
        }
      } catch (e) { authErr = e instanceof Error ? e.message : String(e) }
      res = authErr !== null
        ? { threadId: st.codexThreadId, finalText: '', usage: null, error: authErr, aborted: false }
        : await runTurn({
          cwd: target.cwd, prompt, resumeId: st.codexThreadId, signal: ac.signal,
          onProgress: p => { if (p.kind !== 'message') progress.update(summarizeProgress(p.kind, p.text)) },
          onThreadStarted: id => { st!.codexThreadId = id; writeState(st!) },
        })
    } finally { clearTimeout(timer); progress.close() }
    if (shuttingDown) { log(`[${req.chatId}] turn ${turn} interrupted by shutdown — inflight kept for recovery`); return }
    const secs = Math.round((Date.now() - t0) / 1000)

    if (res.threadId && !st.codexThreadId) st.codexThreadId = res.threadId
    st.inflight = null
    st.lastTurnAt = new Date().toISOString()
    st.lastMessageId = req.messageId
    st.turns = turn
    st.project = target.project

    let ids: string[]
    if (res.error) {
      // 실패·중단은 중간 출력이 있어도 정상 결과로 게시하지 않는다. 있으면 "미완료" 로 표시해 붙인다.
      const authFail = !res.aborted && isAuthError(res.error)
      const why = res.aborted ? `⏱ ${cfg.review.timeoutMin}분 안에 끝나지 않아 중단했습니다. 범위를 좁혀 다시 멘션해 주세요.`
        : authFail ? authFailureNotice(res.error, authSource(cfg.routes)) : `❌ 자비스 실행 실패: ${res.error}`
      if (authFail) void this.opsLog(`⚠️ 자비스 검수 실패 — Codex 로그인 문제 (<#${req.chatId}>): ${res.error.replace(/\s+/g, ' ').trim().slice(0, 200)} · 조치: 원본 Codex 계정 재로그인 후 다시 멘션`).catch(() => {})
      log(`[${req.chatId}] turn ${turn} failed after ${secs}s: ${res.error}${res.finalText ? ` (partial ${res.finalText.length}c)` : ''}`)
      const payload = res.finalText
        ? buildFinal(`${why}\n\n⚠️ 아래는 중단 전까지의 미완료 출력입니다.\n\n${res.finalText}`, { project: target.project, chatId: req.chatId, turn })
        : { content: why }
      ids = await req.poster.postFinal(progressId, payload)
    } else {
      const color = cfg.routes.botColors?.[cfg.botName]
      const payload = buildFinal(res.finalText, { project: target.project, chatId: req.chatId, turn, color })
      ids = await req.poster.postFinal(progressId, payload)
      const u = res.usage ?? {}
      log(`[${req.chatId}] turn ${turn} done in ${secs}s thread=${st.codexThreadId} in=${u.input_tokens ?? '?'} cached=${u.cached_input_tokens ?? '?'} out=${u.output_tokens ?? '?'} answer=${res.finalText.length}c${payload.filePath ? ' file=' + payload.filePath : ''}${res.error ? ' (warn: ' + res.error + ')' : ''}`)
    }
    st.lastPost = { messageIds: ids }
    writeState(st)
  }

  /** lastTurnAt(없으면 createdAt) 이 days 일 넘은 스레드를 정리. 반환: 정리한 chatId 목록 */
  gc(days: number): string[] {
    const cutoff = Date.now() - days * 86_400_000
    const out: string[] = []
    for (const st of listStates()) {
      if (st.inflight) continue
      const last = Date.parse(st.lastTurnAt ?? st.createdAt)
      if (Number.isFinite(last) && last < cutoff) { this.cleanup(st.chatId); out.push(st.chatId) }
    }
    return out
  }

  /** 스레드 삭제·gc·cleanup CLI 시: codex 세션 파일 + 상태 파일 제거 (보관(archive)만으로는 지우지 않는다) */
  cleanup(chatId: string): boolean {
    const st = readState(chatId)
    if (!st) return false
    if (st.codexThreadId) deleteSessionFile(st.codexThreadId)
    deleteState(chatId)
    log(`[${chatId}] cleaned up (codex thread ${st.codexThreadId ?? '-'})`)
    return true
  }
}

// ---------------------------------------------------------------------------
// pidfile
// ---------------------------------------------------------------------------
function pidAlive(pid: number): boolean { try { process.kill(pid, 0); return true } catch { return false } }
/** 우리가 쓴 pidfile 만 지운다 (그 사이 다른 인스턴스가 새로 썼으면 건드리지 않는다). */
function dropPidFile(): void { try { if (readFileSync(PID_FILE, 'utf8').trim() === String(process.pid)) unlinkSync(PID_FILE) } catch {} }
function claimPid(): void {
  if (existsSync(PID_FILE)) {
    const old = parseInt(readFileSync(PID_FILE, 'utf8').trim(), 10)
    if (old && old !== process.pid && pidAlive(old)) { process.stderr.write(`jarvis: 이미 실행 중 (pid ${old}) — ${PID_FILE}\n`); process.exit(2) }
  }
  writeFileSync(PID_FILE, `${process.pid}\n`)
  process.on('exit', dropPidFile)
}
/** SIGTERM/SIGINT: codex 자식을 먼저 정리(SIGTERM → 5초 후 SIGKILL)하고 종료. inflight 상태는 남겨 다음 기동 때 복구한다. */
function installSignalHandlers(): void {
  for (const sig of ['SIGINT', 'SIGTERM'] as const) process.on(sig, () => {
    if (shuttingDown) return
    shuttingDown = true
    const n = inflightChildren()
    log(`${sig} received — stopping (${n} codex child${n === 1 ? '' : 'ren'} in flight; inflight state kept for recovery)`)
    void killAllChildren(5000).then(k => { if (k) log(`killed ${k} codex child process(es)`); dropPidFile(); process.exit(0) })
    setTimeout(() => { dropPidFile(); process.exit(0) }, 7000)
  })
}

// ---------------------------------------------------------------------------
// Discord 모드
// ---------------------------------------------------------------------------
async function runDiscord(cfg: Config, roleVersion: string): Promise<void> {
  const { Client, GatewayIntentBits, ActivityType, Partials, ChannelType } = await import('discord.js')
  const { token, appId } = loadBotEnv(cfg.envFile)
  claimPid()
  const jarvis = new Jarvis(cfg, roleVersion, appId)
  jarvis.trustedBots = loadTrustedBots(cfg.routes)
  log(`trusted bot authors: ${jarvis.trustedBots.size} (${[...jarvis.trustedBots.values()].join(', ')})`)
  const client = new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.DirectMessages, GatewayIntentBits.MessageContent],
    partials: [Partials.Channel],
  })
  jarvis.opsLog = async content => {
    try {
      const ch: any = await client.channels.fetch(cfg.routes.opsLogChannelId || cfg.routes.generalChannelId)
      await ch.send({ content, allowedMentions: { parse: [] } })
    } catch (e) { log(`ops log post failed: ${e}`) }
  }

  const toHist = (m: any): HistMsg => ({
    id: m.id,
    author: m.member?.displayName ?? m.author?.displayName ?? m.author?.username ?? '?',
    isBot: !!m.author?.bot,
    content: stripSelfMention(discordMessageText(m), appId),
    createdAt: new Date(m.createdTimestamp ?? Date.now()).toISOString(),
  })

  const fetchHistory = (channel: any, excludeId: string) => async (after: string | null): Promise<HistMsg[]> => {
    // 요청 메시지 자체와 자비스 자신의 메시지는 제외 (codex 스레드가 이미 자기 답을 갖고 있다). 다른 봇(마크)은 유지.
    const keep = (m: any) => m.id !== excludeId && m.author?.id !== appId
    const out: HistMsg[] = []
    const cap = cfg.review.historyMaxMessages
    if (after) {
      let cursor = after
      while (out.length < cap) {
        const batch: Map<string, any> = await channel.messages.fetch({ after: cursor, limit: 100 })
        if (!batch.size) break
        const arr = [...batch.values()].sort((a, b) => a.createdTimestamp - b.createdTimestamp)
        out.push(...arr.filter(keep).map(toHist))
        cursor = arr[arr.length - 1]!.id
        if (batch.size < 100) break
      }
      return out.slice(0, cap)
    }
    let before: string | undefined = excludeId
    while (out.length < cap) {
      const batch: Map<string, any> = await channel.messages.fetch({ before, limit: 100 })
      if (!batch.size) break
      const arr = [...batch.values()].sort((a, b) => b.createdTimestamp - a.createdTimestamp)
      out.push(...arr.filter(keep).map(toHist))
      before = arr[arr.length - 1]!.id
      if (batch.size < 100) break
    }
    // 스레드면 시작 메시지(부모 채널의 starter) 도 앞에 붙인다
    if (channel.isThread?.()) {
      try { const s = await channel.fetchStarterMessage(); if (s && keep(s)) out.push(toHist(s)) } catch {}
    }
    return out.slice(0, cap).reverse()
  }

  client.once('clientReady', async c => {
    log(`gateway connected as ${c.user.tag} (app ${appId}) model=${cfg.model}/${cfg.effort} max=${cfg.review.maxConcurrent} timeout=${cfg.review.timeoutMin}m role=${roleVersion}`)
    try { c.user.setPresence({ activities: [{ name: '검수 대기 중 — @자비스 로 호출', type: ActivityType.Custom }] }) } catch {}
    // 크래시 복구
    for (const st of listStates()) {
      if (!st.inflight) continue
      let text: string | null = null
      if (st.codexThreadId) text = recoverFinalMessage(st.codexThreadId, st.inflight.startedAt)
      const content = text ? buildFinal(text, { project: st.project, chatId: st.chatId, turn: st.turns + 1 }) : { content: '⚠️ 이전 요청이 중단되었습니다. 다시 멘션해 주세요.' }
      try {
        const ch: any = await client.channels.fetch(st.chatId)
        if (ch) {
          const poster = new DiscordPoster(ch)
          const ids = await poster.postFinal(st.inflight.progressMessageId ?? null, content)
          st.lastPost = { messageIds: ids }
        }
        log(`[${st.chatId}] recovered inflight (${text ? 'posted recovered answer' : 'no answer found'})`)
      } catch (e) { log(`[${st.chatId}] recovery post failed: ${e}`) }
      if (text) { st.turns += 1; st.lastMessageId = st.inflight.messageId }
      st.inflight = null
      writeState(st)
    }
    // 오래된 스레드 상태 정리 (routes.json review.gcDays): 기동 직후 한 번, 이후 하루 한 번
    const runGc = () => { const r = jarvis.gc(cfg.review.gcDays); if (r.length) log(`gc: ${r.length} thread(s) older than ${cfg.review.gcDays}d removed — ${r.join(', ')}`) }
    runGc(); setInterval(runGc, 86_400_000)
  })

  client.on('messageCreate', msg => {
    const gi: GateInput = {
      messageId: msg.id,
      authorId: msg.author.id,
      authorIsBot: msg.author.bot,
      authorIsTrustedBot: msg.author.bot && jarvis.trustedBots.has(msg.author.id),
      guildId: msg.guildId,
      isDirectMessage: msg.channel.type === ChannelType.DM,
      channelId: msg.channelId,
      parentId: msg.channel.isThread() ? msg.channel.parentId : null,
      mentionedUserIds: [...msg.mentions.users.keys()],
    }
    const g = gate(gi, jarvis.gateCfg())
    if (g.action === 'drop') { if (g.reason === 'bot rate limit') log(`[${msg.channelId}] dropped bot brief from ${jarvis.trustedBots.get(msg.author.id)} — rate limit`); return }
    msg.react(cfg.ackEmoji).catch(() => msg.react('👀').catch(() => {}))
    const requesterBot = gi.authorIsTrustedBot ? jarvis.botDisplay(jarvis.trustedBots.get(msg.author.id) ?? 'bot') : undefined
    log(`[${g.chatId}] accepted msg ${msg.id} (${g.isDirectMessage ? 'DM' : msg.channel.isThread() ? 'thread' : 'channel'} of ${g.parentChannelId}${requesterBot ? ', brief from ' + requesterBot : ''})`)
    const poster = new DiscordPoster(msg.channel as any)
    jarvis.enqueue({
      chatId: g.chatId, parentChannelId: g.parentChannelId, messageId: msg.id,
      isDirectMessage: g.isDirectMessage,
      userText: stripSelfMention(msg.content, appId),
      fetchHistory: fetchHistory(msg.channel, msg.id),
      poster, requesterBot,
    }).catch(e => { log(`[${g.chatId}] handle failed: ${e?.stack ?? e}`); poster.postFinal(null, { content: `❌ 자비스 내부 오류: ${String(e).slice(0, 300)}` }).catch(() => {}) })
  })

  client.on('threadDelete', t => { jarvis.cleanup(t.id) })
  // 보관(archive)은 finish-worker 와 Discord 자동 보관(24h)이 일으키므로 기억을 지우지 않는다. 삭제·gc 만 정리.
  client.on('threadUpdate', (o, n) => { if (n.archived && !o.archived && readState(n.id)) log(`[${n.id}] thread archived — state kept (gc after ${cfg.review.gcDays}d)`) })
  client.on('error', e => log(`discord error: ${e}`))
  client.on('shardDisconnect', (_e, id) => log(`shard ${id} disconnected`))
  let reconnectLoggedAt = 0
  client.on('shardReconnecting', id => { if (Date.now() - reconnectLoggedAt > 60_000) { reconnectLoggedAt = Date.now(); log(`shard ${id} reconnecting`) } })
  await client.login(token)
}

// ---------------------------------------------------------------------------
// dry-run
// ---------------------------------------------------------------------------
interface Fixture {
  message?: Partial<GateInput> & { content?: string }
  history?: HistMsg[]
  cwd?: string
  project?: string
  selfAppId?: string
}

async function runDry(cfg: Config, roleVersion: string, args: string[]): Promise<void> {
  const opt = (k: string) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : undefined }
  let fixtures: Fixture[] = []
  const fx = opt('--fixture')
  if (fx) {
    const raw = JSON.parse(fx === '-' ? await Bun.stdin.text() : readFileSync(fx, 'utf8'))
    fixtures = Array.isArray(raw) ? raw : [raw]
  } else {
    fixtures = [{ message: { content: opt('--message') ?? '이 저장소의 현재 상태를 한 줄로 평가해 줘.' } }]
  }
  const cwdArg = opt('--cwd'); const chatArg = opt('--chat')
  const r = cfg.routes
  const selfAppId = fixtures[0]?.selfAppId ?? 'JARVIS-APP'
  const jarvis = new Jarvis(cfg, roleVersion, selfAppId)
  jarvis.trustedBots = loadTrustedBots(cfg.routes)
  jarvis.opsLog = async text => { process.stdout.write(`\n=== [post:ops-log] ===\n${text}\n`) }
  process.stdout.write(`[dry] trusted bots: ${jarvis.trustedBots.size} (${[...jarvis.trustedBots.values()].join(', ')})\n`)
  const poster = new ConsolePoster()
  const jobs = fixtures.map((f, i) => {
    const chatId = f.message?.channelId ?? chatArg ?? `dry-chat-${i + 1}`
    const gi: GateInput = {
      messageId: f.message?.messageId ?? `dry-msg-${Date.now()}-${i}`,
      authorId: f.message?.authorId ?? r.ownerUserId,
      authorIsBot: f.message?.authorIsBot ?? false,
      authorIsTrustedBot: !!f.message?.authorIsBot && jarvis.trustedBots.has(f.message?.authorId ?? ''),
      guildId: f.message?.guildId === null ? null : f.message?.guildId ?? r.guildId,
      isDirectMessage: f.message?.isDirectMessage ?? false,
      channelId: chatId,
      parentId: f.message?.parentId ?? r.generalChannelId,
      mentionedUserIds: f.message?.mentionedUserIds ?? [selfAppId],
    }
    const g = gate(gi, jarvis.gateCfg())
    process.stdout.write(`[dry] fixture ${i + 1}: gate → ${JSON.stringify(g)}\n`)
    if (g.action === 'drop') return Promise.resolve()
    const cwd = f.cwd ?? cwdArg
    const override: Partial<Target> | undefined = cwd ? { cwd, project: f.project ?? basename(cwd), route: null, registry: null } : undefined
    return jarvis.enqueue({
      chatId: g.chatId, parentChannelId: g.parentChannelId, messageId: gi.messageId,
      isDirectMessage: g.isDirectMessage,
      userText: stripSelfMention(f.message?.content ?? '', selfAppId),
      fetchHistory: async () => f.history ?? [],
      poster, targetOverride: override,
      requesterBot: gi.authorIsTrustedBot ? jarvis.botDisplay(jarvis.trustedBots.get(gi.authorId) ?? 'bot') : undefined,
    })
  })
  await Promise.all(jobs)
}

// ---------------------------------------------------------------------------
async function main() {
  const args = process.argv.slice(2)
  const cfg = loadConfig()
  if (!roleEnabled(cfg.routes, '리뷰어') && !['cleanup', 'gc'].includes(args[0] ?? '')) {
    process.stdout.write('자비스: 비활성\n'); return
  }
  ensureDirs()
  if (args[0] === 'cleanup') {
    const roleVersion = 'n/a'; const j = new Jarvis(cfg, roleVersion, 'cli')
    const which = args[1]
    if (!which) { process.stderr.write('usage: server.ts cleanup <chatId>|--all\n'); process.exit(1) }
    const targets = which === '--all' ? listStates().map(s => s.chatId) : [which]
    for (const t of targets) process.stdout.write(`${t}: ${j.cleanup(t) ? 'removed' : 'no state'}\n`)
    return
  }
  if (args[0] === 'gc') {
    const i = args.indexOf('--days'); const days = i >= 0 ? Number(args[i + 1]) : cfg.review.gcDays
    if (!Number.isFinite(days) || days <= 0) { process.stderr.write('usage: server.ts gc [--days N]\n'); process.exit(1) }
    const j = new Jarvis(cfg, 'n/a', 'cli'); const removed = j.gc(days)
    process.stdout.write(`gc: ${removed.length} thread(s) older than ${days}d removed${removed.length ? ' — ' + removed.join(', ') : ''}\n`)
    return
  }
  if (args[0] === 'reauth') { prepareCodexHome(cfg, { reauth: true }); process.stdout.write('auth.json re-linked to configured source\n'); return }
  const roleVersion = prepareCodexHome(cfg)
  installSignalHandlers()
  if (args.includes('--dry-run')) { await runDry(cfg, roleVersion, args); return }
  await runDiscord(cfg, roleVersion)
}

main().catch(e => { log(`fatal: ${e?.stack ?? e}`); process.exit(1) })

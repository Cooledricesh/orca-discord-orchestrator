/** Grok worker bridge (design §5): one bot in one thread ↔ one grok session, one `grok -p` process per turn.
 * Started by spawn-worker.sh: `exec bun grok-worker/bridge.ts <THREADS_DIR>/<tid>.json`. finish-worker.sh TERMs it. */
import { Client, Events, GatewayIntentBits, type Message } from 'discord.js'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { homedir } from 'node:os'
import { randomUUID } from 'node:crypto'
import { mentionsOtherBotOnly } from '../plugin/discord-orca/gate-helpers.ts'
import { downloadAttachment, readBotToken, sendChunked } from '../bridge-kit/discord-io.ts'
import { ProgressCard, keepTyping, type ProgressRef } from '../bridge-kit/progress.ts'
import { grokArgs, grokEnv } from './args.ts'
import { buildIsolatedHome } from './home.ts'
import { GrokRun, classify, exitLabel, nextTurn, patchJSON, pruneTurns, writeJSONAtomic } from './turn.ts'

type Registry = { threadId?: string; guildId: string; ownerUserId: string; jarvisAppId?: string; bot: string; path: string; promptFile: string
  sessionId: string; model?: string; effort?: string; resumed?: boolean; discordStateDir: string; rolesFile: string; ackEmoji?: string; sandbox?: string }
type Status = 'starting' | 'ready' | 'auth_failed' | 'degraded' | 'failed' | 'stopped'
type Runtime = { pid: number; status: Status; sessionStarted: boolean; sessionId: string; updatedAt: number; lastError?: string; progress?: ProgressRef }

const NO_OUTPUT_MS = Number(process.env.GROK_NO_OUTPUT_MS) || 60 * 60_000
const registryFile = process.argv[2]
if (!registryFile) { console.error('usage: bun bridge.ts <state/threads/<tid>.json>'); process.exit(2) }
const rt = registryFile.replace(/\.json$/, '') + '.grok', runtimeFile = join(rt, 'runtime.json')
mkdirSync(rt, { recursive: true, mode: 0o700 })
const previous = (() => { try { return JSON.parse(readFileSync(runtimeFile, 'utf8')) as Partial<Runtime> } catch { return {} } })()
const runtime: Runtime = { pid: process.pid, status: 'starting', sessionStarted: false, sessionId: '', updatedAt: Date.now() }
const saveRuntime = () => { runtime.updatedAt = Date.now(); try { writeJSONAtomic(runtimeFile, runtime) } catch (e) { console.error(`runtime.json 저장 실패: ${errMsg(e)}`) } }
function setStatus(status: Status, lastError?: string) { runtime.status = status; if (lastError) runtime.lastError = lastError; else delete runtime.lastError; saveRuntime() }
const errMsg = (e: unknown) => e instanceof Error ? e.message : String(e)
let client: Client | undefined
function fatal(reason: string): never {
  console.error(`⛔ ${reason}`); setStatus('failed', reason)
  void Promise.resolve(client?.destroy()).catch(() => {}); process.exit(1)
}

let record: Registry
try { record = JSON.parse(readFileSync(registryFile, 'utf8')) } catch { fatal('등록부를 읽지 못했습니다') }
for (const k of ['guildId', 'ownerUserId', 'bot', 'path', 'promptFile', 'sessionId', 'discordStateDir', 'rolesFile'] as const)
  if (typeof record[k] !== 'string' || !record[k]) fatal(`등록부 필드 없음: ${k}`)
const tid = record.threadId || process.env.ORCA_THREAD_ID || basename(registryFile, '.json')
const root = process.env.ORCH_ROOT ?? join(import.meta.dir, '..')
const activityFile = process.env.DISCORD_ACTIVITY_FILE ?? registryFile.replace(/\.json$/, '.activity')
const users = [record.ownerUserId, record.jarvisAppId].filter((u): u is string => !!u)
// First turn: --session-id for a new task, --resume when spawn --resume or this bridge already created the session.
runtime.sessionId = record.sessionId
runtime.sessionStarted = !!record.resumed || (previous.sessionStarted === true && previous.sessionId === record.sessionId)
saveRuntime()

// Isolation is mandatory (§4.3): without it grok would load the Claude Discord plugin and join the bot gateway twice.
const realHome = process.env.HOME || homedir()
let env: Record<string, string>
try { env = grokEnv({ ...process.env, ORCA_THREAD_ID: tid }, buildIsolatedHome(realHome, join(rt, 'home')), process.env.GROK_HOME || join(realHome, '.grok')) }
catch (e) { fatal(`격리 HOME 생성 실패: ${errMsg(e)}`) }
if (!existsSync(record.rolesFile)) fatal(`역할 파일 없음: ${record.rolesFile}`)
if (!existsSync(record.path)) fatal(`작업 경로 없음: ${record.path}`)
let token: string
try { token = readBotToken(record.discordStateDir) } catch { fatal('봇 토큰 없음 (DISCORD_STATE_DIR/.env)') }

let activityAt = 0
function activity(force = false) {
  if (!force && Date.now() - activityAt < 10_000) return; activityAt = Date.now()
  try { writeFileSync(activityFile, `${new Date().toISOString()} ${tid}\n`) } catch {}
}
function touchRegistry(extra: Record<string, unknown> = {}) {
  try { patchJSON(registryFile, v => { Object.assign(v, extra); v.lastActivityAt = new Date().toISOString() }) } catch (e) { console.error(`등록부 갱신 실패: ${errMsg(e)}`) }
}
function detached(cmd: string, args: string[]) {
  try { spawn(cmd, args, { detached: true, stdio: 'ignore', env: process.env }).unref() } catch (e) { console.error(`${basename(cmd)} 실행 실패: ${errMsg(e)}`) }
}
const opsLog = (line: string) => detached('zsh', ['-c', 'source "$1/bin/lib.sh" && ops_log "$2" "$3"', '_', root, record.bot, line])
const finishWorker = (status: 'succeeded' | 'stopped') => detached('nohup', [join(root, 'bin/finish-worker.sh'), tid, status])

client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent] })
const discord = client
async function thread(): Promise<any> {
  const ch: any = await discord.channels.fetch(tid)
  if (!ch || ch.guildId !== record.guildId || !('messages' in ch)) throw Error('Thread outside guild')
  return ch
}
async function say(text: string) { try { await sendChunked(await thread(), text, { users }) } catch (e) { console.error(`Discord 전송 실패: ${errMsg(e)}`) } }
function accepts(m: Message) {
  if (m.guildId !== record.guildId || m.channelId !== tid || m.webhookId || m.author.bot || m.author.id !== record.ownerUserId) return false
  return !mentionsOtherBotOnly([...m.mentions.parsedUsers.values()].map(u => ({ id: u.id, bot: u.bot })), discord.user?.id)
}
const card = new ProgressCard({
  post: async (_chat, content) => (await (await thread()).send({ content, allowedMentions: { parse: [] } })).id,
  edit: async (_chat, id, content) => { await (await thread()).messages.edit(id, { content, allowedMentions: { parse: [] } }) },
  remove: async (_chat, id) => { await (await thread()).messages.delete(id) },
}, { onChange: ref => { runtime.progress = ref; saveRuntime() } })

// Turns are serialized; messages that arrive mid-turn are batched into the next turn (grok -p cannot be steered).
type Item = { kind: 'startup' } | { kind: 'msg'; text: string }
const queue: Item[] = [{ kind: 'startup' }]
let current: GrokRun | undefined, currentTool = '', pumping = false, finishing = false, closing = false, failStreak = 0
const waiting = () => queue.filter(i => i.kind === 'msg').length
async function pump() {
  if (pumping) return; pumping = true
  try {
    while (queue.length && !finishing && !closing) {
      if (queue[0].kind === 'startup') { queue.shift(); await turn(readFileSync(record.promptFile, 'utf8'), true); continue }
      const texts = queue.splice(0).flatMap(i => i.kind === 'msg' ? [i.text] : [])
      await turn(texts.join('\n\n---\n\n'), false)
    }
  } catch (e) { console.error(`턴 처리 오류: ${errMsg(e)}`); await say(`❌ 브리지 오류: ${errMsg(e).slice(0, 200)}`) }
  finally { pumping = false }
  if (queue.length && !finishing && !closing) void pump()
}
async function turn(input: string, startup: boolean, recovery = false): Promise<void> {
  const n = nextTurn(rt), base = join(rt, `turn-${n}`), promptFile = `${base}.prompt.md`
  writeFileSync(promptFile, input, { mode: 0o600 })
  const resume = runtime.sessionStarted
  const args = grokArgs({ promptFile, cwd: record.path, sessionId: record.sessionId, resume, rules: readFileSync(record.rolesFile, 'utf8'),
    model: record.model, effort: record.effort, sandbox: record.sandbox })
  console.log(`▶ 턴 ${n}${startup ? ' (기동)' : ''} ${resume ? '--resume' : '--session-id'} ${record.sessionId}`)
  const stopTyping = keepTyping(async () => (await thread()).sendTyping())
  const run = current = new GrokRun({ bin: process.env.GROK_BIN || 'grok', args, env, cwd: record.path, logFile: `${base}.ndjson`, errFile: `${base}.err`,
    noOutputMs: NO_OUTPUT_MS, onOutput: () => activity(),
    onEvent: e => { if (e.kind === 'tool') { currentTool = e.desc; console.log(`· ${e.name}`); void card.step(tid, e.desc) } } })
  const r = await run.result
  current = undefined; stopTyping(); await card.end(); pruneTurns(rt)
  if (closing) return   // SIGTERM from finish-worker: no failure notice for the group we just killed
  if (r.stream.events && !runtime.sessionStarted) { runtime.sessionStarted = true; saveRuntime() }
  const end = r.stream.end
  touchRegistry({ lastTurn: { exit: r.code ?? r.signal, costUsd: end?.cost ?? null, totalTokens: end?.usage.total_tokens ?? null, endedAt: new Date().toISOString() } })
  console.log(`■ 턴 ${n} exit ${exitLabel(r)} · 도구 ${r.stream.tools} · ${Math.round(r.ms / 1000)}s${end?.cost !== undefined ? ` · $${end.cost.toFixed(4)}` : ''}`)
  const o = classify(r, resume)
  switch (o.kind) {
    case 'ok':
      failStreak = 0; if (runtime.status !== 'ready') setStatus('ready')
      if (o.text) try { await sendChunked(await thread(), o.text, { users }) } catch (e) { console.error(`답 게시 실패: ${errMsg(e)}`) }
      return
    case 'interrupted': return   // 중단 posts its own status
    case 'timeout': return say('⚠️ 60분 동안 출력이 없어 중단했습니다.')
    case 'parse':
      await say(`⚠️ Grok 응답을 해석하지 못했습니다 (exit ${exitLabel(r)}). 로그: ${base}.ndjson`)
      if (o.text) await say(o.text)
      return
    case 'resume_missing': {
      if (recovery) break
      const old = record.sessionId; record.sessionId = randomUUID()
      touchRegistry({ sessionId: record.sessionId, previousSessionIds: [...readPrevious(), old] })
      runtime.sessionId = record.sessionId; runtime.sessionStarted = false; saveRuntime()
      await say(`⚠️ 이전 Grok 세션(\`${old}\`)을 찾지 못해 대화 맥락 없이 새 세션으로 이어 갑니다. 기동 정보를 다시 넣었습니다.`)
      return turn(startup ? input : `${readFileSync(record.promptFile, 'utf8')}\n\n---\n\n${input}`, true, true)
    }
    case 'session_exists':
      if (recovery) break
      runtime.sessionStarted = true; saveRuntime(); return turn(input, startup, true)
    case 'auth':
      setStatus('auth_failed', 'grok login required')
      await say('⛔ Grok 로그인이 만료됐습니다. 관리 터미널에서 `grok login` 후 같은 메시지를 다시 보내세요.')
      opsLog(`⛔ ${record.bot} Grok 로그인 만료 — 관리 터미널에서 grok login 필요 (${tid})`)
      return
    case 'failed': break
  }
  failStreak++
  await say(`❌ Grok 실행 실패 (exit ${exitLabel(r)}): ${o.kind === 'failed' && o.detail ? o.detail : o.kind}`)
  if (failStreak === 3) opsLog(`❌ ${record.bot} Grok 실행 3회 연속 실패 (${tid})`)
}
function readPrevious(): string[] { try { const v = JSON.parse(readFileSync(registryFile, 'utf8')).previousSessionIds; return Array.isArray(v) ? v : [] } catch { return [] } }
const elapsed = (ms: number) => { const s = Math.round(ms / 1000); return s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초` }
// 종료/중단 are handled here, never by the model: finish closes the terminal, which would kill the bridge mid-reply.
async function endSession() {
  finishing = true; queue.length = 0
  await say('⏹ 세션 종료'); finishWorker('succeeded')
}
async function stopWork() {
  finishing = true; queue.length = 0
  const run = current, tool = currentTool
  if (run) {
    run.interrupt()
    await Promise.race([run.result, Bun.sleep(10_000)])
    await run.terminate()   // stragglers in the group (background shell tools ignore SIGINT)
  }
  await say(run ? `⏹ 작업을 중단했습니다.\n마지막 도구: ${tool || '없음'} · 경과 ${elapsed(Date.now() - run.started)}` : '⏹ 중단했습니다.\n진행 중인 작업 없음')
  finishWorker('stopped')
}
let inbound: Promise<void> = Promise.resolve()
async function onMessage(m: Message) {
  if (closing || finishing || !accepts(m)) return
  void m.react(record.ackEmoji || '👀').catch(() => {})
  activity(true); touchRegistry()
  const word = m.content.trim()
  if (word === '종료') return endSession()
  if (word === '중단') return stopWork()
  let text = m.content
  for (const att of m.attachments.values()) {
    try { text += `\n첨부: ${await downloadAttachment(att, join(record.discordStateDir, 'inbox'))}` }
    catch (e) { text += `\n첨부 실패: ${att.name} (${errMsg(e)})` }
  }
  queue.push({ kind: 'msg', text })
  if (current) card.setSuffix(` (+${waiting()} 대기)`)
  void pump()
}
discord.on(Events.MessageCreate, m => { inbound = inbound.then(() => onMessage(m)).catch(e => console.error(`메시지 처리 오류: ${errMsg(e)}`)) })

// Gateway health: discord.js reconnects by itself; more than 5 minutes down → degraded (status.sh ⚠️).
let downSince = 0
const down = () => { downSince ||= Date.now() }, up = () => { downSince = 0; if (runtime.status === 'degraded') setStatus('ready') }
discord.on(Events.ShardDisconnect, down); discord.on(Events.ShardReconnecting, down)
discord.on(Events.ShardResume, up); discord.on(Events.ShardReady, up)
setInterval(() => { if (downSince && Date.now() - downSince > 5 * 60_000 && runtime.status === 'ready') setStatus('degraded', 'Discord 게이트웨이 5분 넘게 끊김') }, 30_000)

async function shutdown(signal: string) {
  if (closing) return; closing = true
  console.log(`${signal}: grok 정리 후 종료`)
  if (current) await current.terminate()
  await Promise.race([card.end(), Bun.sleep(2000)])
  runtime.status = 'stopped'; saveRuntime()
  await Promise.resolve(discord.destroy()).catch(() => {})
  process.exit(0)
}
for (const s of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(s, () => void shutdown(s))
process.on('uncaughtException', e => { console.error(`브리지 예외: ${errMsg(e)}`); current?.kill('SIGTERM'); setStatus('failed', errMsg(e)); process.exit(1) })
process.on('unhandledRejection', e => console.error(`처리되지 않은 거부: ${errMsg(e)}`))

discord.once(Events.ClientReady, async () => {
  console.log(`● ${record.bot} 연결됨 — 스레드 ${tid}, 세션 ${record.sessionId}${runtime.sessionStarted ? ' (재개)' : ''}`)
  if (previous.progress?.messageId) try { await (await thread()).messages.delete(previous.progress.messageId) } catch {}
  setStatus('ready'); void pump()
})
discord.login(token).catch(e => fatal(`Discord 로그인 실패: ${e instanceof Error ? e.name : 'Error'}`))

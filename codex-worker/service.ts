/** Vision bridge: one Discord bot ↔ one Codex app-server thread. Started by control.ts. */
type DynamicToolCallResponse = { success: boolean; contentItems: { type: 'inputText'; text: string }[] }
import { spawn, type ChildProcess } from 'node:child_process'
import { readFileSync, openSync, closeSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { SocketRPC } from './rpc.ts'
import { Once, InputRouter, type Envelope, type Ledger } from './core.ts'
import { DiscordPort, toolSpecs } from './discord.ts'
import { ProgressCard } from '../bridge-kit/progress.ts'
import { requiredJSON, PersistenceError, readRegistry, type Runtime, writeJSON, runtimePaths } from './storage.ts'

const registryFile = process.argv[2]
if (!registryFile) throw Error('Registry file required')
const record = readRegistry(registryFile)
const paths = runtimePaths(registryFile)
const activityFile = registryFile.replace(/\.json$/, '.activity')
const root = process.env.ORCH_ROOT ?? join(import.meta.dir, '..')
const sd = record.discordStateDir
const discord = new DiscordPort({ channel: record.channelId, guild: record.guildId, owner: record.ownerUserId, stateDir: sd, privateRoots: [dirname(registryFile), dirname(sd)] })
const ledger = existsSync(paths.ledger) ? requiredJSON<Ledger>(paths.ledger) : {}
const once = new Once(ledger, () => writeJSON(paths.ledger, ledger))
const startingInbox: import('discord.js').Message[] = []
let deliver: ((m: import('discord.js').Message) => Promise<void>) | undefined
discord.client.on('messageCreate', m => { if (deliver) void deliver(m); else startingInbox.push(m) })
let child: ChildProcess | undefined, rpc: SocketRPC | undefined, router: InputRouter | undefined
let stage = 'initializing'
let closing = false, healthy = false, threadId = ''
const failedTurns = new Set<string>(), completedTurns = new Set<string>()
const known = new Set<string>(), active = new Map<string, string>()
const runtime: Runtime = { pid: process.pid, generation: record.generation, status: 'starting', updatedAt: Date.now() }
mkdirSync(sd, { recursive: true, mode: 0o700 })
const controlToken = randomUUID()
// Captured before the first saveRuntime() overwrites the previous generation's file.
const leftoverProgress = (() => { try { return existsSync(paths.runtime) ? requiredJSON<Runtime>(paths.runtime).progress : undefined } catch { return undefined } })()
let activityAt = 0
function activity(force = false) { if (!force && Date.now() - activityAt < 1000) return; activityAt = Date.now(); writeFileSync(activityFile, new Date().toISOString()) }
let savedRuntime = ''
function saveRuntime() { runtime.activeTurns = Object.fromEntries(active); const value = JSON.stringify(runtime); if (value === savedRuntime) return; writeJSON(paths.runtime, { ...runtime, updatedAt: Date.now() }); savedRuntime = value }
/** Progress card per main-thread turn (bridge-kit): posted to the chat the input came from. Bridge-only; the model never sees it. */
let progressChat = record.channelId
let pendingChat: string | undefined
const progress = new ProgressCard({
  post: async (chat, content) => (await discord.send(content, chat)).id,
  edit: async (chat, id, content) => { const ch = await discord.channel(chat); await ch.messages.edit(id, { content, allowedMentions: { parse: [] } }) },
  remove: deleteMessage,
}, { onChange: ref => { runtime.progress = ref; saveRuntime() } })
function describeItem(item: any): string | undefined {
  const rel = (p: string) => p.startsWith(record.path + '/') ? p.slice(record.path.length + 1) : p
  switch (item?.type) {
    case 'commandExecution': return '`' + String(item.command ?? '').replace(/`/g, "'") + '`'
    case 'fileChange': return 'Edit ' + (item.changes ?? []).map((c: any) => rel(String(c.path ?? ''))).join(', ')
    case 'mcpToolCall': return `${item.server}.${item.tool}`
    case 'webSearch': return 'WebSearch ' + String(item.query ?? '')
    case 'imageView': return 'View ' + rel(String(item.path ?? ''))
    case 'collabAgentToolCall': return `↳ 서브 에이전트 ${item.tool}${item.prompt ? ' ' + String(item.prompt).replace(/\s+/g, ' ') : ''}`
    case 'subAgentActivity': return `↳ 서브 에이전트 ${item.kind}`
    default: return undefined   // agentMessage/reasoning/plan/dynamicToolCall(discord_*) are visible or silent by design
  }
}
const progressStep = (sub: boolean, text: string) => progress.step(progressChat, (sub ? '↳ ' : '') + text)
const progressEnd = () => progress.end()
async function deleteMessage(chat: string, id: string) { try { const ch = await discord.channel(chat); await ch.messages.delete(id) } catch {} }
function failure(error: unknown) {
  const category = error instanceof Error ? error.name : 'Error'
  const code = typeof (error as any)?.code === 'number' ? (error as any).code : undefined
  runtime.failure = { stage, category, code }; console.error(`Vision failure at ${stage}: ${category}${code === undefined ? '' : ` code=${code}`}`)
  void shutdown(1)
}
async function shutdown(code = 0) {
  if (closing) return; closing = true; healthy = false
  runtime.status = code ? 'failed' : 'stopped'; try { saveRuntime() } catch { console.error('Shutdown persistence failed'); code = 1 }
  await Promise.resolve(discord.client.destroy()).catch(() => {})
  if (rpc && child) {
    for (const [tid, turnId] of active) await rpc.call('turn/interrupt', { threadId: tid, turnId }).catch(() => {})
    for (const tid of known) await rpc.call('thread/backgroundTerminals/clean', { threadId: tid }).catch(() => {})
    rpc.ws.close()
  }
  if (child?.pid) {
    try { process.kill(-child.pid, 'SIGTERM') } catch {}
    await Promise.race([new Promise(r => child!.once('exit', r)), Bun.sleep(1500)])
    try { process.kill(-child.pid, 'SIGKILL') } catch {}
  }
  process.exit(code)
}
process.on('SIGTERM', () => void shutdown()); process.on('SIGINT', () => void shutdown())
process.on('uncaughtException', failure); process.on('unhandledRejection', failure)

const control = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(req) {
  if (req.headers.get('authorization') !== `Bearer ${controlToken}`) return new Response('Denied', { status: 403 })
  const p = new URL(req.url).pathname
  if (p === '/health') return Response.json({ healthy, threadId, generation: record.generation })
  if (p === '/startup-timeout') { runtime.failure = { stage, category: 'StartupTimeout', reason: 'readiness deadline exceeded' }; setTimeout(() => void shutdown(1), 10); return Response.json({ stopping: true }) }
  if (p === '/stop' && req.method === 'POST') { setTimeout(() => void shutdown(), 10); return Response.json({ stopping: true }) }
  return new Response('Not found', { status: 404 })
} })
runtime.controlUrl = `http://127.0.0.1:${control.port}`; runtime.controlToken = controlToken
saveRuntime()

async function event(m: Envelope) {
  const p = m.params ?? {}
  // Child agents spawned by the main session share the Discord tools.
  if (p.item?.type === 'subAgentActivity' && known.has(p.threadId) && p.item.agentThreadId) known.add(p.item.agentThreadId)
  if (m.method === 'thread/started' && p.thread?.parentThreadId && known.has(p.thread.parentThreadId)) known.add(p.thread.id)
  if (p.item?.type === 'collabAgentToolCall' && known.has(p.threadId)) for (const id of p.item.receiverThreadIds ?? []) known.add(id)
  if (m.id !== undefined && m.method === 'currentTime/read') { rpc!.respond(m.id, { currentTimeAt: Math.floor(Date.now() / 1000) }); return }
  if (!known.has(p.threadId)) return
  activity(m.method === 'turn/started' || m.method === 'turn/completed'); router?.event(m)
  if (m.method === 'turn/started' && p.threadId === threadId && pendingChat) { progressChat = pendingChat; pendingChat = undefined }
  if (m.method === 'item/started' && healthy) { const d = describeItem(p.item); if (d) void progressStep(p.threadId !== threadId, d) }
  if (m.method === 'turn/completed' && p.threadId === threadId) await progressEnd()
  if (m.method === 'turn/started' && !completedTurns.has(JSON.stringify([p.threadId, p.turn.id]))) active.set(p.threadId, p.turn.id)
  if (m.method === 'turn/completed') {
    completedTurns.add(JSON.stringify([p.threadId, p.turn.id]))
    if (active.get(p.threadId) === p.turn.id) active.delete(p.threadId)
    if (p.threadId === threadId && ['failed', 'interrupted'].includes(p.turn.status) && !failedTurns.has(p.turn.id)) {
      failedTurns.add(p.turn.id)
      await once.run(`notice:${p.threadId}:${p.turn.id}`, async () => {
        try { await discord.send(`작업이 ${p.turn.status === 'failed' ? '실패' : '중단'}했습니다. 터미널을 확인한 뒤 다시 지시해 주세요.`); return { delivered: true } }
        catch { return { delivered: false } }
      })
    }
  }
  if (m.id !== undefined && m.method === 'item/tool/call') {
    const result = await once.run(`tool:${p.threadId}:${p.callId}`, async (): Promise<DynamicToolCallResponse> => {
      if (active.get(p.threadId) !== p.turnId) return { success: false, contentItems: [{ type: 'inputText', text: 'Stale tool request; no action taken' }] }
      try { return { success: true, contentItems: [{ type: 'inputText', text: await discord.tool(p.tool, p.arguments) }] } }
      catch (e) { return { success: false, contentItems: [{ type: 'inputText', text: `Discord tool failed: ${e instanceof Error ? e.message : e}` }] } }
    })
    rpc!.respond(m.id, result)
  } else if (m.id !== undefined) {
    // Approvals never happen (approvalPolicy: never). Questions/elicitations go through discord_reply instead.
    rpc!.reject(m.id, -32601, 'Not handled by the Discord bridge; ask the user with discord_reply or use the terminal')
  }
  saveRuntime()
}

try {
  const reservation = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: () => new Response('') })
  runtime.url = `ws://127.0.0.1:${reservation.port}`; reservation.stop(true)
  const fd = openSync(paths.serverLog, 'a', 0o600)
  // Inherits the real HOME/CODEX_HOME/config/MCP/skills, like a hand-started codex.
  child = spawn(process.env.CODEX_BIN ?? 'codex', ['app-server', '--listen', runtime.url], { cwd: record.path, env: process.env, detached: true, stdio: ['ignore', fd, fd] })
  closeSync(fd); runtime.appServerPid = child.pid; saveRuntime()
  child.on('error', failure); child.on('exit', () => { if (!closing) { const e = new Error(); e.name = 'AppServerExited'; failure(e) } })
  for (let i = 0; i < 100; i++) {
    try { rpc = await SocketRPC.connect(runtime.url); break } catch { await Bun.sleep(100) }
  }
  if (!rpc) throw Error('App-server unavailable')
  rpc.onClose = () => { if (!closing) { const e = new Error(); e.name = 'AppServerDisconnected'; failure(e) } }
  rpc.onEvent = m => { void event(m).catch(failure) }
  stage = 'Discord login'; await discord.client.login(discord.token())
  const overrides = { cwd: record.path, approvalPolicy: 'never', sandbox: 'danger-full-access',
    ...(record.model ? { model: record.model } : {}), ...(record.effort ? { config: { model_reasoning_effort: record.effort } } : {}),
    developerInstructions: readFileSync(join(root, 'roles/비전.md'), 'utf8') }
  if (leftoverProgress) await deleteMessage(leftoverProgress.chat, leftoverProgress.messageId)
  stage = 'thread start/resume'
  const result = await rpc.call(record.resumed ? 'thread/resume' : 'thread/start', { ...overrides, ...(record.resumed ? { threadId: record.sessionId } : {}), dynamicTools: toolSpecs })
  threadId = result.thread.id; known.add(threadId); runtime.threadId = threadId
  saveRuntime(); router = new InputRouter(threadId, rpc, once, record.effort)
  const handleMessage = async (m: import('discord.js').Message) => {
    if (!discord.accepts(m)) return
    // Receipt signals live in the bridge, not the model: 👀 + typing on accept, ⚠️ if the app-server refused.
    void m.react('👀').catch(() => {})
    if ('sendTyping' in m.channel) void m.channel.sendTyping().catch(() => {})
    pendingChat = m.channelId; if (!active.has(threadId)) progressChat = m.channelId
    try {
      const text = JSON.stringify({ source: 'discord', chat_id: m.channelId, message_id: m.id, text: m.content,
        attachments: [...m.attachments.values()].map(a => ({ name: a.name, size: a.size, contentType: a.contentType })) })
      activity(); await router!.input(m.id, text)
    } catch (e) {
      if (e instanceof PersistenceError) { failure(e); return }
      void m.reactions.cache.get('👀')?.users.remove(discord.client.user!.id).catch(() => {}); void m.react('⚠️').catch(() => {})
      await discord.send('입력을 전달하지 못했습니다. 터미널 상태를 확인한 뒤 다시 보내 주세요.', m.channelId).catch(() => {})
    }
  }
  stage = 'initial turn'
  await router.input(`startup:${record.generation}`, readFileSync(record.promptFile, 'utf8'))
  while (startingInbox.length) await handleMessage(startingInbox.shift()!)
  deliver = handleMessage
  stage = 'running'; runtime.status = 'ready'; healthy = true; saveRuntime()
  setInterval(() => {
    if (active.size) { activity(); void discord.channel(progressChat).then(ch => ch.sendTyping?.()).catch(() => {}) }
    saveRuntime()
  }, 5000)
} catch (e) { failure(e) }

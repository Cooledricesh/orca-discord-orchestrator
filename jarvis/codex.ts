// Codex CLI 호출 (codex exec --json / exec resume <id> --json). 단일 코드 경로.
// CODEX_HOME=state/jarvis/codex-home 로 격리. auth.json 은 ~/.codex 에서 복사(600), 절대 출력하지 않는다.
import { copyFileSync, existsSync, readFileSync, writeFileSync, chmodSync, readdirSync, statSync, rmSync, mkdirSync } from 'fs'
import { join } from 'path'
import { createHash } from 'crypto'
import { CODEX_HOME, authSource, ROLE_FILE, type Config } from './config.ts'
import { log } from './state.ts'

/** config.toml + auth.json 준비. 반환값은 roleVersion (역할 지침 sha256 앞 12자). */
export function prepareCodexHome(cfg: Config, opts: { reauth?: boolean } = {}): string {
  mkdirSync(CODEX_HOME, { recursive: true })
  chmodSync(CODEX_HOME, 0o700)
  const role = readFileSync(ROLE_FILE, 'utf8')
  const roleVersion = createHash('sha256').update(role).digest('hex').slice(0, 12)
  const toml = [
    `# 자동 생성 (jarvis/codex.ts). 직접 고치지 말 것 — roles/자비스.md 와 routes.json 을 고친다.`,
    ...(cfg.model ? [`model = ${JSON.stringify(cfg.model)}`] : []),
    ...(cfg.effort ? [`model_reasoning_effort = ${JSON.stringify(cfg.effort)}`] : []),
    `sandbox_mode = "read-only"`,
    `approval_policy = "never"`,
    `developer_instructions = ${JSON.stringify(role)}`,
    ``,
  ].join('\n')
  writeFileSync(join(CODEX_HOME, 'config.toml'), toml)
  const source = authSource(cfg.routes)
  if (!existsSync(source)) throw new Error(`Codex 인증 파일 없음: ${source} — codex login 또는 codexAuthFile 설정`)
  // auth.json 은 없을 때만 복사한다. codex 가 OAuth 토큰을 갱신해 CODEX_HOME/auth.json 에 되쓰므로
  // 매번 덮어쓰면 오래된 refresh token 이 들어갈 수 있다. 강제 복사는 `server.ts reauth`.
  const dst = join(CODEX_HOME, 'auth.json')
  if (opts.reauth || !existsSync(dst)) {
    copyFileSync(source, dst)
    chmodSync(dst, 0o600)
    log(`auth.json ${opts.reauth ? 're-copied (reauth)' : 'copied'} from configured auth source`)
  }
  return roleVersion
}

export interface TurnProgress { kind: 'command' | 'reasoning' | 'message'; text: string }
export interface TurnResult {
  threadId: string | null
  finalText: string
  usage: { input_tokens?: number; cached_input_tokens?: number; output_tokens?: number } | null
  error: string | null
  aborted: boolean
}

export interface TurnOpts {
  cwd: string
  prompt: string
  resumeId: string | null
  signal: AbortSignal
  onProgress?: (p: TurnProgress) => void
  /** thread.started 직후 호출 — 상태에 codexThreadId 를 바로 기록해 크래시 복구가 세션 파일을 찾을 수 있게 한다. */
  onThreadStarted?: (threadId: string) => void
}

function codexEnv(): Record<string, string> {
  return {
    PATH: process.env.PATH ?? '/usr/bin:/bin',
    HOME: process.env.HOME ?? '',
    CODEX_HOME,
    LANG: process.env.LANG ?? 'en_US.UTF-8',
    TERM: 'dumb',
    NO_COLOR: '1',
  }
}

/** 실행 중인 codex 자식 프로세스 (graceful shutdown 용). */
const children = new Set<Bun.Subprocess>()
/** 모든 자식에 SIGTERM, 5초 뒤 SIGKILL. 전부 끝나면 resolve. */
export async function killAllChildren(graceMs = 5000): Promise<number> {
  const procs = [...children]
  if (!procs.length) return 0
  for (const p of procs) { try { p.kill('SIGTERM') } catch {} }
  const deadline = Date.now() + graceMs
  while (children.size && Date.now() < deadline) await Bun.sleep(100)
  for (const p of [...children]) { try { p.kill('SIGKILL') } catch {} }
  return procs.length
}
export function inflightChildren(): number { return children.size }

/** 한 턴 실행. 프롬프트는 stdin 으로 넘긴다 (인자 길이 제한/인용 문제 회피). */
export async function runTurn(o: TurnOpts): Promise<TurnResult> {
  const base = o.resumeId
    ? [process.env.CODEX_BIN ?? 'codex', 'exec', 'resume', o.resumeId, '--json', '--skip-git-repo-check', '-']
    : [process.env.CODEX_BIN ?? 'codex', 'exec', '--json', '--skip-git-repo-check', '-C', o.cwd, '-']
  const res: TurnResult = { threadId: o.resumeId, finalText: '', usage: null, error: null, aborted: false }
  let proc: Bun.Subprocess<'pipe', 'pipe', 'pipe'>
  try {
    proc = Bun.spawn(base, { cwd: o.cwd, env: codexEnv(), stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' })
  } catch (e) {
    res.error = `codex 실행 실패: ${e}`
    return res
  }
  children.add(proc)
  void proc.exited.finally(() => children.delete(proc))
  const onAbort = () => { res.aborted = true; try { proc.kill('SIGTERM') } catch {} ; setTimeout(() => { try { proc.kill('SIGKILL') } catch {} }, 5000) }
  if (o.signal.aborted) onAbort(); else o.signal.addEventListener('abort', onAbort, { once: true })
  try {
    proc.stdin.write(o.prompt)
    proc.stdin.end()
  } catch (e) { res.error = `stdin 쓰기 실패: ${e}` }

  let lastAgentMessage = ''
  const stderrP = new Response(proc.stderr).text()
  const reader = proc.stdout.getReader()
  const dec = new TextDecoder()
  let buf = ''
  const handle = (line: string) => {
    if (!line.trim()) return
    let ev: any
    try { ev = JSON.parse(line) } catch { return }
    switch (ev.type) {
      case 'thread.started': res.threadId = ev.thread_id ?? res.threadId; if (res.threadId && !o.resumeId) o.onThreadStarted?.(res.threadId); break
      case 'item.completed': {
        const it = ev.item ?? {}
        if (it.type === 'agent_message' && typeof it.text === 'string') { lastAgentMessage = it.text; o.onProgress?.({ kind: 'message', text: it.text }) }
        else if (it.type === 'command_execution') o.onProgress?.({ kind: 'command', text: String(it.command ?? '') })
        else if (it.type === 'reasoning' && typeof it.text === 'string') o.onProgress?.({ kind: 'reasoning', text: it.text })
        break
      }
      case 'item.started': {
        const it = ev.item ?? {}
        if (it.type === 'command_execution') o.onProgress?.({ kind: 'command', text: String(it.command ?? '') })
        break
      }
      case 'turn.completed': res.usage = ev.usage ?? null; break
      case 'turn.failed': res.error = ev.error?.message ?? JSON.stringify(ev.error ?? ev); break
      case 'error': res.error = ev.message ?? JSON.stringify(ev); break
    }
  }
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i: number
    while ((i = buf.indexOf('\n')) >= 0) { handle(buf.slice(0, i)); buf = buf.slice(i + 1) }
  }
  if (buf.trim()) handle(buf)
  await proc.exited
  o.signal.removeEventListener('abort', onAbort)
  const stderr = await stderrP
  res.finalText = lastAgentMessage
  if (!res.aborted && proc.exitCode !== 0 && !res.error) {
    res.error = `codex 종료 코드 ${proc.exitCode}: ${stderr.trim().split('\n').slice(-3).join(' | ').slice(0, 400)}`
  }
  if (res.aborted && !res.error) res.error = '시간 초과로 중단'
  return res
}

// --- 세션 파일 (CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl) -----
export function findSessionFile(threadId: string): string | null {
  const root = join(CODEX_HOME, 'sessions')
  if (!existsSync(root)) return null
  const stack = [root]
  while (stack.length) {
    const d = stack.pop()!
    let ents: string[] = []
    try { ents = readdirSync(d) } catch { continue }
    for (const e of ents) {
      const p = join(d, e)
      let st
      try { st = statSync(p) } catch { continue }
      if (st.isDirectory()) stack.push(p)
      else if (e.endsWith('.jsonl') && e.includes(threadId)) return p
    }
  }
  return null
}

/** inflight 시각 이후 완료된 턴의 마지막 agent 메시지를 세션 파일에서 복구. */
export function recoverFinalMessage(threadId: string, sinceIso: string): string | null {
  const f = findSessionFile(threadId)
  if (!f) return null
  const since = Date.parse(sinceIso) / 1000
  let found: string | null = null
  for (const line of readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue
    let d: any
    try { d = JSON.parse(line) } catch { continue }
    if (d.type !== 'event_msg') continue
    const p = d.payload ?? {}
    if (p.type === 'task_complete' && typeof p.last_agent_message === 'string' && (p.started_at ?? 0) >= since - 5) found = p.last_agent_message
  }
  return found
}

export function deleteSessionFile(threadId: string): boolean {
  const f = findSessionFile(threadId)
  if (!f) return false
  try { rmSync(f, { force: true }); log(`session file deleted: ${f}`); return true } catch { return false }
}

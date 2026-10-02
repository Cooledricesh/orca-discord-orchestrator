// Codex CLI 호출 (codex exec --json / exec resume <id> --json). 단일 코드 경로.
// CODEX_HOME=state/jarvis/codex-home 로 격리. auth.json 은 원본 인증 파일(authSource)로의 심볼릭 링크, 절대 출력하지 않는다.
import { existsSync, readFileSync, writeFileSync, chmodSync, readdirSync, statSync, rmSync, mkdirSync, lstatSync, realpathSync, symlinkSync, renameSync, type Stats } from 'fs'
import { dirname, join, resolve } from 'path'
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
  // auth.json 은 원본으로의 링크. 복사본은 원본과 refresh token 을 따로 들고 있다가 먼저 갱신한 쪽이 다른 쪽을
  // 무효화했다(refresh token already used / 401). codex 는 auth.json 을 제자리에 다시 쓰고(링크 따라감, rename 없음)
  // 갱신 전 파일을 다시 읽으므로 한 파일을 공유하면 복사본의 별도 토큰 계보가 없어진다. 단 프로세스 간 동시 갱신은
  // 직렬화되지 않는다(codex 파일 잠금 없음) — 같은 홈에서 codex 세션 여럿을 돌릴 때와 같은 조건. 강제 재링크는 `server.ts reauth`.
  ensureAuthLink(cfg, opts)
  return roleVersion
}

export type AuthLink = 'ok' | 'linked' | 'kept-newer'

/** last_refresh 시각(ms). 읽기·파싱 실패는 NaN (비교하면 항상 "새것 아님"). 내용은 출력하지 않는다. */
function lastRefresh(file: string): number {
  try { return Date.parse(JSON.parse(readFileSync(file, 'utf8')).last_refresh) } catch { return NaN }
}

/** dst 를 source 로의 심볼릭 링크로 맞춘다. 순수 함수(로그 없음). 원본보다 새 일반 파일은 force 없이 건드리지 않는다. */
export function linkAuthFile(source: string, dst: string, force = false): AuthLink {
  source = resolve(source) // 존재 확인과 링크 대상이 같은 절대 경로여야 한다 (상대 경로는 dst 기준으로 풀려 끊긴 링크가 된다)
  if (!existsSync(source)) throw new Error(`Codex 인증 파일 없음: ${source} — codex login 또는 codexAuthFile 설정`)
  // 실제 경로가 같으면 그대로: 올바른 링크, 원본이 자비스 자신의 파일(전용 로그인), 원본이 dst 를 가리키는 별칭 링크. 끊긴 링크는 throw → 재링크
  try { if (realpathSync(dst) === realpathSync(source)) return 'ok' } catch {}
  let st: Stats | null = null
  try { st = lstatSync(dst) } catch {}
  if (st?.isFile() && !force && lastRefresh(dst) > lastRefresh(source)) return 'kept-newer'
  // 임시 이름에 링크를 만든 뒤 rename 으로 원자 교체 (끊긴/엉뚱한 링크, 오래된 복사본 모두)
  const tmp = `${dst}.link-${process.pid}`
  rmSync(tmp, { force: true })
  symlinkSync(source, tmp)
  renameSync(tmp, dst)
  return 'linked'
}

/** CODEX_HOME/auth.json 링크 확인·복구 (매 검수 턴 전, 기동 시, reauth). 원본 없으면 throw. */
export function ensureAuthLink(cfg: Config, opts: { reauth?: boolean } = {}): AuthLink {
  const r = linkAuthFile(authSource(cfg.routes), join(CODEX_HOME, 'auth.json'), opts.reauth)
  if (r === 'linked') log(`auth.json linked to configured auth source${opts.reauth ? ' (reauth)' : ''}`)
  else if (r === 'kept-newer') log(`auth.json is a regular file newer than configured auth source — kept, not linked (reauth to force)`)
  return r
}

/** Codex 로그인·인증 실패 여부 (스레드 안내·#운영-로그 알림용). */
export function isAuthError(msg: string): boolean {
  // 401 은 HTTP/인증 문맥에서만 (스택 트레이스의 parser.ts:401:12 같은 줄 번호 오탐 방지)
  return /unauthorized|\(401\)|(?:status|http)\D{0,12}401\b|refresh token|sign in again|not logged in|인증 파일 없음/i.test(msg)
}

/** 인증 실패 시 스레드에 남길 안내 (Discord 마크다운). detached: 자비스가 링크가 아닌 원본보다 새 별도 파일을 쓰는 중('kept-newer'). */
export function authFailureNotice(error: string, source: string, detached = false): string {
  const tail = detached
    ? `자비스는 지금 원본보다 새 별도 인증 파일을 쓰고 있으므로, 재로그인 후 오케스트레이터 폴더에서 \`zsh -c 'source ./bin/lib.sh; bun "$ORCH_ROOT/jarvis/server.ts" reauth'\` 를 실행해 주세요.`
    : `자비스는 그 인증 파일을 직접 쓰므로 재시작·reauth 는 필요 없습니다.`
  return [
    `❌ Codex 로그인 문제로 검수에 실패했습니다.`,
    `- 오류: ${error.replace(/\s+/g, ' ').trim().slice(0, 300)}`,
    `- 조치: 원본 Codex 계정에 다시 로그인한 뒤 같은 요청을 다시 멘션해 주세요 (Orca 에서 해당 Codex 계정 재로그인, 또는 터미널에서 \`CODEX_HOME="${dirname(source)}" codex login\`). ${tail}`,
  ].join('\n')
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

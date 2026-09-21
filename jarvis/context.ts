// 컨텍스트 수집: 라우트/등록부 → 작업 디렉터리, git 상태, Discord 이력 포맷.
import { existsSync, readFileSync, accessSync, constants, mkdirSync } from 'fs'
import { join } from 'path'
import { REGISTRY_DIR, GENERAL_CWD, type Routes, type Route } from './config.ts'

export interface RegistryEntry {
  threadId?: string; channelId?: string; project?: string; path?: string; projectPath?: string
  bot?: string; taskTitle?: string; baseRef?: string; status?: string; worktreeMode?: string
}

export function readRegistry(threadId: string): RegistryEntry | null {
  const f = join(REGISTRY_DIR, `${threadId}.json`)
  if (!existsSync(f)) return null
  try { return JSON.parse(readFileSync(f, 'utf8')) as RegistryEntry } catch { return null }
}

export interface Target {
  project: string
  cwd: string
  route: Route | null
  registry: RegistryEntry | null
  /** #프라이데이 스레드: 코드 대상 없음 (cwd 는 빈 디렉터리) */
  general?: boolean
  directMessage?: boolean
  error?: string
}

/** 스레드(chatId)와 부모 채널로 프로젝트/작업 디렉터리를 정한다. */
export function resolveTarget(routes: Routes, chatId: string, parentChannelId: string, directMessage = false): Target {
  if (directMessage) {
    mkdirSync(GENERAL_CWD, { recursive: true, mode: 0o700 })
    return { project: 'dm', cwd: GENERAL_CWD, route: null, registry: null, general: true, directMessage: true }
  }
  const route = routes.routes[parentChannelId] ?? null
  const registry = chatId !== parentChannelId ? readRegistry(chatId) : null
  if (!registry && !route && parentChannelId === routes.generalChannelId) {
    mkdirSync(GENERAL_CWD, { recursive: true, mode: 0o700 })
    return { project: 'general', cwd: GENERAL_CWD, route: null, registry: null, general: true }
  }
  const project = registry?.project ?? route?.name ?? 'unknown'
  const cwd = registry?.path ?? route?.path ?? ''
  const t: Target = { project, cwd, route, registry }
  if (!cwd) t.error = '대상 프로젝트 경로를 찾지 못했습니다 (routes.json 에 없는 채널).'
  else if (!existsSync(cwd)) t.error = `대상 경로가 없습니다: ${cwd}`
  else {
    try { accessSync(cwd, constants.R_OK) } catch { t.error = `대상 경로를 읽을 수 없습니다: ${cwd}` }
  }
  return t
}

async function run(cwd: string, args: string[], maxChars = 6000): Promise<string> {
  try {
    // 10초 타임아웃: index.lock 대기 같은 곳에서 큐 전체가 멈추지 않게. stderr 는 읽지 않으므로 버린다.
    const p = Bun.spawn(args, { cwd, stdout: 'pipe', stderr: 'ignore', timeout: 10_000 })
    const out = await new Response(p.stdout).text()
    await p.exited
    if (p.exitCode !== 0) return ''
    return out.length > maxChars ? out.slice(0, maxChars) + `\n… (${out.length - maxChars}자 생략)` : out
  } catch { return '' }
}

/** git 상태 블록. baseRef 가 있으면 diff --stat, 없으면 log -5. 저장소가 아니면 빈 문자열. */
export async function gitBlock(cwd: string, baseRef?: string): Promise<string> {
  const inside = (await run(cwd, ['git', 'rev-parse', '--is-inside-work-tree'])).trim()
  if (inside !== 'true') return ''
  const parts: string[] = []
  const branch = (await run(cwd, ['git', 'rev-parse', '--abbrev-ref', 'HEAD'])).trim()
  const head = (await run(cwd, ['git', 'rev-parse', '--short', 'HEAD'])).trim()
  parts.push(`branch: ${branch} @ ${head}`)
  const status = await run(cwd, ['git', 'status', '--short'])
  parts.push(`git status --short:\n${status.trim() || '(clean)'}`)
  if (baseRef) {
    const stat = await run(cwd, ['git', 'diff', '--stat', baseRef])
    parts.push(`git diff --stat ${baseRef.slice(0, 12)} (작업 시작 시점 대비):\n${stat.trim() || '(no diff)'}`)
  } else {
    const lg = await run(cwd, ['git', 'log', '-5', '--oneline'])
    parts.push(`git log -5 --oneline:\n${lg.trim()}`)
  }
  return parts.join('\n')
}

export interface HistMsg { id: string; author: string; isBot: boolean; content: string; createdAt: string }

/** 메시지 본문뿐 아니라 마크의 결과 카드도 검수 컨텍스트에 보존한다. */
export function discordMessageText(message: {
  content?: string | null
  embeds?: ReadonlyArray<{ title?: string | null; description?: string | null; fields?: ReadonlyArray<{ name: string; value: string }> }>
  attachments?: { size: number }
}): string {
  const parts = [message.content ?? '']
  for (const embed of message.embeds ?? []) {
    const text = [embed.title, embed.description, ...(embed.fields ?? []).map(f => `${f.name}: ${f.value}`)].filter(Boolean).join('\n')
    if (text) parts.push(`[카드]\n${text}`)
  }
  if (message.attachments?.size) parts.push(`[첨부 ${message.attachments.size}개]`)
  return parts.filter(Boolean).join('\n')
}

/** Discord 이력 → 텍스트 (오래된 것 먼저, 문자 수 제한은 뒤쪽=최신을 우선 보존). */
export function formatHistory(msgs: HistMsg[], maxChars: number): string {
  const lines = msgs.map(m => {
    const t = m.createdAt.slice(5, 16).replace('T', ' ')
    const body = m.content.trim() || '(첨부/빈 메시지)'
    return `[${t}] ${m.author}${m.isBot ? ' (bot)' : ''}: ${body}`
  })
  let out = lines.join('\n')
  if (out.length > maxChars) out = '… (앞부분 생략)\n' + out.slice(out.length - maxChars)
  return out
}

export interface ContextParts {
  target: Target
  history: string
  git: string
  isFirstTurn: boolean
  channelLevel: boolean
  /** general 대상일 때 나열할 프로젝트 목록 */
  knownRoutes?: Route[]
  /** 요청자가 봇이면 표시 이름 (프롬프트에 '요청자:' 줄을 붙인다) */
  requesterBot?: string
}

/** 라우트의 읽기 경로: writeDir 이 있으면 그 하위만 (document → orchestrator/docs, 루트가 아님). */
export function routeReadPath(r: Route): string {
  return r.writeDir ? join(r.path, r.writeDir).replace(/\/+$/, '') : r.path
}

/** 첫 턴은 전체 컨텍스트, 이후 턴은 delta. 사용자 메시지 뒤에 붙일 블록을 만든다. */
export function buildPrompt(userText: string, c: ContextParts): string {
  const { target } = c
  const head: string[] = []
  if (target.general) {
    head.push(target.directMessage
      ? '소유자와의 비공개 DM. 멘션 없이 질문에 답한다. 대상 프로젝트가 불분명하면 물어보고, DM 내용을 서버 채널에 자동으로 공개하지 않는다.'
      : '코드 대상 없음 — #프라이데이 기획/논의 스레드. 대화 내용 자체를 검토·검증·second opinion 한다.')
    head.push('알려진 프로젝트 (대화가 특정 프로젝트를 다루면 그 경로를 직접 읽어도 된다, 읽기 전용):')
    for (const r of c.knownRoutes ?? []) head.push(`- ${r.name}: ${routeReadPath(r)}`)
  } else {
    head.push(`프로젝트: ${target.project}`)
    head.push(`작업 디렉터리: ${target.cwd}`)
  }
  if (target.registry) {
    const r = target.registry
    head.push(`작업자 등록부: bot=${r.bot ?? '?'} title=${r.taskTitle ?? '?'} status=${r.status ?? '?'} worktree=${r.worktreeMode ?? '?'}${r.projectPath && r.projectPath !== r.path ? ` (원본 ${r.projectPath})` : ''}${r.baseRef ? ` baseRef=${r.baseRef.slice(0, 12)}` : ''}`)
  }
  if (target.route?.writeDir) head.push(`이 프로젝트의 쓰기 허용 범위는 ${target.route.writeDir} 아래뿐이다.`)
  if (c.channelLevel && !target.directMessage) head.push(`이 요청은 채널 최상위(스레드 아님)에서 왔다. 대상이 불분명하면 범위를 물어라.`)

  const ctxTitle = c.isFirstTurn ? '## 컨텍스트 (Discord 스레드 이력)' : '## 컨텍스트 갱신 (이전 턴 이후 새 메시지)'
  const gitTitle = c.isFirstTurn ? '## git' : '## git (현재)'
  const blocks = [
    `## 요청\n${c.requesterBot ? `요청자: ${c.requesterBot} (봇 브리프 — 항목별로 판정)\n` : ''}${userText.trim() || '(본문 없음 — 스레드 상황을 검토하고 의견을 달라)'}`,
    `## 대상\n${head.join('\n')}`,
    `${ctxTitle}\n${c.history.trim() || '(없음)'}`,
  ]
  if (c.git) blocks.push(`${gitTitle}\n${c.git}`)
  return blocks.join('\n\n')
}

// 설정 로딩: routes.json, 봇 env, 경로 상수. 토큰은 절대 로그/출력하지 않는다.
import { readFileSync, existsSync, mkdirSync } from 'fs'
import { homedir } from 'os'
import { dirname, join, resolve } from 'path'
import { fileURLToPath } from 'url'

export const JARVIS_DIR = dirname(fileURLToPath(import.meta.url))
export const ORCH_ROOT = process.env.ORCH_ROOT ?? resolve(JARVIS_DIR, '..')
export const STATE_ROOT = join(ORCH_ROOT, 'state')
export const JARVIS_STATE = join(STATE_ROOT, 'jarvis')
export const THREAD_STATE_DIR = join(JARVIS_STATE, 'threads')
export const CODEX_HOME = join(JARVIS_STATE, 'codex-home')
/** #프라이데이 스레드용 빈 작업 디렉터리 (코드 대상 없음). git 저장소 밖(~/.jarvis)에 둔다 — orchestrator 안이면 그 git 상태가 프롬프트에 샌다. */
export const GENERAL_CWD = join(homedir(), '.jarvis', 'general-cwd')
export const PID_FILE = join(STATE_ROOT, 'jarvis.pid')
export const LOG_FILE = join(STATE_ROOT, 'log', 'jarvis.log')
export const REGISTRY_DIR = join(STATE_ROOT, 'threads')
export const ROLE_FILE = join(ORCH_ROOT, 'roles', '자비스.md')
export const RUNS_DIR = join(ORCH_ROOT, 'runs')
export const ROUTES_FILE = join(ORCH_ROOT, 'routes.json')
export const USER_CODEX_AUTH = join(homedir(), '.codex', 'auth.json')

export interface Route { name: string; path: string; writeDir?: string; kind?: 'lounge'; bot?: string }
export interface ReviewCfg { maxConcurrent: number; timeoutMin: number; historyMaxMessages: number; historyMaxChars: number; channelThreadTtlHours: number; gcDays: number }
export interface Routes {
  routes: Record<string, Route>
  generalChannelId: string
  guildId: string
  ownerUserId: string
  bots: Record<string, string | string[]>
  botDisplay?: Record<string, string>
  botColors?: Record<string, number>
  models?: Record<string, unknown>
  emojis?: Record<string, string>
  review?: Partial<ReviewCfg>
  stateRoot?: string
}

export interface Config {
  routes: Routes
  botName: string
  envFile: string
  review: ReviewCfg
  model: string
  effort: string
  ackEmoji: string
}

export function loadRoutes(file = ROUTES_FILE): Routes {
  return JSON.parse(readFileSync(file, 'utf8')) as Routes
}

export function loadConfig(): Config {
  const routes = loadRoutes()
  const botName = (routes.bots['리뷰어'] as string | undefined) ?? '자비스'
  const stateRoot = (routes.stateRoot ?? '~/.claude/channels').replace(/^~/, homedir())
  const envFile = join(stateRoot, 'bots', `${botName}.env`)
  const r = routes.review ?? {}
  const review: ReviewCfg = {
    maxConcurrent: r.maxConcurrent ?? 4,
    timeoutMin: r.timeoutMin ?? 20,
    historyMaxMessages: r.historyMaxMessages ?? 200,
    historyMaxChars: r.historyMaxChars ?? 40000,
    channelThreadTtlHours: r.channelThreadTtlHours ?? 6,
    gcDays: r.gcDays ?? 30,
  }
  const m = (routes.models?.['리뷰어'] ?? {}) as { model?: string; effort?: string }
  return {
    routes,
    botName,
    envFile,
    review,
    model: m.model ?? 'gpt-6-astra',
    effort: m.effort ?? 'xhigh',
    ackEmoji: routes.emojis?.ack ?? '👀',
  }
}

/** ~/.claude/channels/bots/<봇>.env 에서 DISCORD_BOT_TOKEN / DISCORD_APP_ID 를 읽는다. 값은 반환만 하고 기록하지 않는다. */
export function loadBotEnv(file: string): { token: string; appId: string } {
  if (!existsSync(file)) throw new Error(`봇 env 파일 없음: ${file} (Discord 앱을 만들고 DISCORD_BOT_TOKEN/DISCORD_APP_ID 를 넣어야 한다)`)
  const out: Record<string, string> = {}
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    const mm = line.match(/^(\w+)=(.*)$/)
    if (mm) out[mm[1]!] = mm[2]!.replace(/^"|"$/g, '').trim()
  }
  if (!out.DISCORD_BOT_TOKEN) throw new Error(`${file}: DISCORD_BOT_TOKEN 없음`)
  if (!out.DISCORD_APP_ID) throw new Error(`${file}: DISCORD_APP_ID 없음`)
  return { token: out.DISCORD_BOT_TOKEN, appId: out.DISCORD_APP_ID }
}

/** 신뢰 봇(프라이데이 + 마크1..4)의 DISCORD_APP_ID 만 읽는다 (토큰 줄은 읽지 않는다). 반환: appId → 봇 이름 */
export function loadTrustedBots(routes: Routes, botsDir = join((routes.stateRoot ?? '~/.claude/channels').replace(/^~/, homedir()), 'bots')): Map<string, string> {
  const names: string[] = []
  const c = routes.bots['상담역']; if (typeof c === 'string') names.push(c)
  const w = routes.bots['workers']; if (Array.isArray(w)) names.push(...w)
  const out = new Map<string, string>()
  for (const n of new Set(names)) {
    try {
      for (const line of readFileSync(join(botsDir, `${n}.env`), 'utf8').split('\n')) {
        const m = line.match(/^DISCORD_APP_ID=(.*)$/)
        if (m) { out.set(m[1]!.replace(/^"|"$/g, '').trim(), n); break }
      }
    } catch (e) {
      // 조용히 넘기면 그 봇의 브리프가 전부 'untrusted bot author' 로 버려진다 — 반드시 보이게
      process.stderr.write(`jarvis: 신뢰 봇 env 읽기 실패 (${n}): ${e}\n`)
    }
  }
  return out
}

export function ensureDirs(): void {
  for (const d of [JARVIS_STATE, THREAD_STATE_DIR, CODEX_HOME, join(STATE_ROOT, 'log')]) mkdirSync(d, { recursive: true })
  mkdirSync(GENERAL_CWD, { recursive: true, mode: 0o700 })
}

// 스레드별 상태 state/jarvis/threads/<chatId>.json + 로그.
import { readFileSync, writeFileSync, existsSync, readdirSync, rmSync, renameSync, statSync, mkdirSync, appendFileSync, truncateSync } from 'fs'
import { join, dirname } from 'path'
import { THREAD_STATE_DIR, LOG_FILE } from './config.ts'

export interface ThreadState {
  chatId: string
  codexThreadId: string | null
  project: string
  cwd: string
  lastMessageId: string | null
  roleVersion: string
  createdAt: string
  lastTurnAt: string | null
  turns: number
  inflight: { messageId: string; startedAt: string; progressMessageId?: string } | null
  lastPost: { messageIds: string[] }
  /** 처리한 사용자 메시지 id (최근 200개) */
  processed: string[]
}

const PROCESSED_CAP = 200

export function stateFile(chatId: string): string { return join(THREAD_STATE_DIR, `${chatId}.json`) }

export function readState(chatId: string): ThreadState | null {
  const f = stateFile(chatId)
  if (!existsSync(f)) return null
  try { return JSON.parse(readFileSync(f, 'utf8')) as ThreadState } catch { return null }
}

export function writeState(s: ThreadState): void {
  const f = stateFile(s.chatId)
  mkdirSync(dirname(f), { recursive: true })
  const tmp = `${f}.tmp`
  writeFileSync(tmp, JSON.stringify(s, null, 2) + '\n')
  renameSync(tmp, f)
}

export function newState(chatId: string, project: string, cwd: string, roleVersion: string): ThreadState {
  return {
    chatId, codexThreadId: null, project, cwd, lastMessageId: null, roleVersion,
    createdAt: new Date().toISOString(), lastTurnAt: null, turns: 0, inflight: null,
    lastPost: { messageIds: [] }, processed: [],
  }
}

export function markProcessed(s: ThreadState, messageId: string): void {
  if (!s.processed.includes(messageId)) s.processed.push(messageId)
  if (s.processed.length > PROCESSED_CAP) s.processed.splice(0, s.processed.length - PROCESSED_CAP)
}

export function isProcessed(chatId: string, messageId: string): boolean {
  const s = readState(chatId)
  return !!s && s.processed.includes(messageId)
}

export function listStates(): ThreadState[] {
  if (!existsSync(THREAD_STATE_DIR)) return []
  const out: ThreadState[] = []
  for (const f of readdirSync(THREAD_STATE_DIR)) {
    if (!f.endsWith('.json')) continue
    try { out.push(JSON.parse(readFileSync(join(THREAD_STATE_DIR, f), 'utf8')) as ThreadState) } catch {}
  }
  return out
}

export function deleteState(chatId: string): void {
  rmSync(stateFile(chatId), { force: true })
}

// --- 로그 (10MB 넘으면 truncate). 파일이 유일한 기록이고, 전경 TTY 에서만 stderr 에도 보여 준다 ---
const LOG_MAX = 10 * 1024 * 1024
export function log(msg: string): void {
  const line = `${new Date().toISOString()} ${msg}\n`
  try {
    mkdirSync(dirname(LOG_FILE), { recursive: true })
    try { if (statSync(LOG_FILE).size > LOG_MAX) truncateSync(LOG_FILE, 0) } catch {}
    appendFileSync(LOG_FILE, line)
  } catch {}
  if (process.stderr.isTTY) process.stderr.write(line)
}

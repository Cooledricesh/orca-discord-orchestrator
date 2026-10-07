// orca fork: 툴 게이트 확인 요청(bin/tool-gate.py request_approval)의 승인/거부 버튼.
// 게이트가 state/tool-gate/<스레드>.approvals.json 에 남긴 항목에 소유자 클릭을 기록한다.
// 게이트는 매 호출 때 이 파일을 읽으므로 approved 가 있으면 60분(APPROVAL_TTL_S) 허용한다.
import { readFileSync, writeFileSync, renameSync, mkdirSync } from 'fs'
import { join, dirname } from 'path'

export const GATE_BUTTON = /^tgate:(allow|deny):([0-9a-f]{64})$/

export type GateClick = 'approved' | 'denied' | 'missing'

export function gateApprovalsPath(env: NodeJS.ProcessEnv = process.env): string | null {
  const thread = env.ORCA_THREAD_ID
  const root = env.STATE_DIR_ROOT || (env.ORCH_ROOT ? join(env.ORCH_ROOT, 'state') : '')
  return thread && /^\d+$/.test(thread) && root ? join(root, 'tool-gate', `${thread}.approvals.json`) : null
}

export function gateOwner(env: NodeJS.ProcessEnv = process.env): string {
  try {
    const routes = JSON.parse(readFileSync(env.ROUTES_FILE ?? '', 'utf8'))
    return String(routes?.ownerUserId ?? '')
  } catch {
    return ''
  }
}

/** 항목이 있고 같은 메시지에서 눌렸을 때만 기록한다. 거부는 기록만 (게이트는 계속 막는다). */
export function recordGateClick(path: string, key: string, messageId: string, behavior: 'allow' | 'deny',
                                now = Math.floor(Date.now() / 1000)): GateClick {
  let data: Record<string, any>
  try {
    data = JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return 'missing'
  }
  const entry = data?.[key]
  if (!entry || typeof entry !== 'object' || String(entry.messageId) !== messageId) return 'missing'
  if (behavior === 'allow') entry.approved = now
  else entry.denied = now
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.${process.pid}.tmp`
  writeFileSync(tmp, JSON.stringify(data), { mode: 0o600 })
  renameSync(tmp, path)
  return behavior === 'allow' ? 'approved' : 'denied'
}

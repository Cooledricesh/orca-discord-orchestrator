import { readFileSync, writeFileSync, renameSync, mkdirSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
export class PersistenceError extends Error {}
export function requiredJSON<T>(path: string): T {
  try { const value = JSON.parse(readFileSync(path, 'utf8')); if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error(); return value }
  catch { throw new PersistenceError(`Missing or corrupt required state: ${path}`) }
}
export function writeJSON(path: string, value: any) {
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
    const tmp = `${path}.${process.pid}.tmp`; writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 }); renameSync(tmp, path)
  } catch { throw new PersistenceError(`Cannot persist state: ${path}`) }
}
/** Private runtime next to the registry: state/vision.json → state/vision.codex/ */
export function runtimePaths(registryFile: string) {
  const root = join(dirname(registryFile), basename(registryFile, '.json') + '.codex')
  return { root, runtime: join(root, 'runtime.json'), ledger: join(root, 'ledger.json'), serverLog: join(root, 'server.log'), bridgeLog: join(root, 'bridge.log') }
}
export interface Registry {
  channelId: string; guildId: string; ownerUserId: string
  bot: string; path: string; promptFile: string; generation: string; discordStateDir: string
  status?: 'active' | 'stopped' | 'failed'
  model?: string; effort?: string; resumed?: boolean; sessionId?: string
}
export interface Runtime {
  pid: number; generation: string; status: 'starting' | 'ready' | 'failed' | 'stopped'
  updatedAt?: number; appServerPid?: number; threadId?: string; activeTurns?: Record<string, string>
  controlUrl?: string; controlToken?: string; url?: string
  progress?: { chat: string; messageId: string }
  failure?: { stage: string; category: string; code?: number; reason?: string }
}
export function readRegistry(path: string): Registry {
  const r = requiredJSON<Registry>(path)
  for (const key of ['channelId','guildId','ownerUserId','bot','path','promptFile','generation','discordStateDir'] as const) {
    if (typeof r[key] !== 'string' || !r[key]) throw new PersistenceError(`Invalid registry field: ${key}`)
  }
  if (!/^\d+$/.test(r.channelId)) throw new PersistenceError('Invalid Discord channel ID')
  return r
}
export function readRuntime(path: string): Runtime {
  const r = requiredJSON<Runtime>(path)
  if (!Number.isInteger(r.pid) || r.pid <= 0 || typeof r.generation !== 'string' || !r.generation || !['starting','ready','failed','stopped'].includes(r.status)) throw new PersistenceError('Runtime lacks process ownership evidence')
  if (r.status === 'ready' && (!r.appServerPid || !r.threadId || !r.controlUrl || !r.controlToken)) throw new PersistenceError('Ready runtime lacks backend identity')
  if (r.controlUrl !== undefined && !/^http:\/\/127\.0\.0\.1:\d+$/.test(r.controlUrl)) throw new PersistenceError('Invalid control endpoint')
  if (r.appServerPid !== undefined && (!Number.isInteger(r.appServerPid) || r.appServerPid <= 0 || typeof r.url !== 'string' || !/^ws:\/\/127\.0\.0\.1:\d+$/.test(r.url))) throw new PersistenceError('Invalid app-server ownership evidence')
  return r
}

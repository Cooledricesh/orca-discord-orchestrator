/** Lifecycle for the Vision bridge: start|stop|health|attach <registry.json>. Never prints credentials. */
import { spawn } from 'node:child_process'
import { openSync, closeSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { requiredJSON, readRegistry, readRuntime, type Registry, type Runtime, runtimePaths } from './storage.ts'
const [verb, file] = process.argv.slice(2)
if (!file) throw Error('Usage: control.ts start|stop|health|attach <registry.json>')
const paths = runtimePaths(file)
const record = verb === 'stop' ? requiredJSON<Partial<Registry>>(file) : readRegistry(file)
const emptyRuntime: Runtime = { pid: 0, generation: '', status: 'starting' }
function runtimeSnapshot() { try { return readRuntime(paths.runtime) } catch { return emptyRuntime } }
let runtime = runtimeSnapshot()
async function request(path: string, method = 'GET') {
  if (!runtime.controlUrl || runtime.generation !== record.generation) throw Error('No matching service')
  return fetch(runtime.controlUrl + path, { method, headers: { authorization: `Bearer ${runtime.controlToken}` }, signal: AbortSignal.timeout(2000) })
}
async function healthy() { try { const r = await (await request('/health')).json() as any; return r.healthy && r.generation === record.generation } catch { return false } }
const isService = () => runtime.pid && Bun.spawnSync(['ps', '-p', String(runtime.pid), '-o', 'command=']).stdout.toString().includes(`service.ts ${file}`)
const serverAlive = () => {
  if (!runtime.appServerPid) return false
  try { process.kill(-runtime.appServerPid, 0); return true } catch (e) { return (e as NodeJS.ErrnoException).code !== 'ESRCH' }
}
if (verb === 'start') {
  if (await healthy()) throw Error('Service already running')
  if (isService() || serverAlive()) throw Error('Previous service still alive; stop it first')
  mkdirSync(paths.root, { recursive: true, mode: 0o700 })
  const fd = openSync(paths.bridgeLog, 'a', 0o600)
  const proc = spawn(process.execPath, [join(import.meta.dir, 'service.ts'), file], { detached: true, stdio: ['ignore', fd, fd], env: process.env })
  closeSync(fd); proc.unref()
  for (let i = 0; i < Math.ceil(Number(process.env.CODEX_WORKER_START_TIMEOUT_MS ?? 30000) / 200); i++) {
    await Bun.sleep(200); runtime = runtimeSnapshot()
    if (await healthy()) { runtime = runtimeSnapshot(); console.log(runtime.threadId); process.exit(0) }
    if (runtime.generation === record.generation && runtime.status === 'failed') break
  }
  if (runtime.generation === record.generation) await request('/startup-timeout', 'POST').catch(() => {})
  throw Error('Vision did not become ready; see bridge.log / server.log')
} else if (verb === 'health') process.exit(await healthy() ? 0 : 1)
else if (verb === 'stop') {
  runtime = runtimeSnapshot()
  if (!runtime.pid) process.exit(0)
  await request('/stop', 'POST').catch(() => {})
  for (let i = 0; i < 100 && isService(); i++) await Bun.sleep(100)
  if (isService()) { try { process.kill(runtime.pid, 'SIGKILL') } catch {} }
  // If the bridge crashed, its detached app-server group still needs cleanup.
  if (runtime.appServerPid) {
    const command = Bun.spawnSync(['ps', '-p', String(runtime.appServerPid), '-o', 'command=']).stdout.toString()
    if (command.includes('app-server') && runtime.url && command.includes(runtime.url)) {
      try { process.kill(-runtime.appServerPid, 'SIGTERM') } catch {}
      await Bun.sleep(500)
      try { process.kill(-runtime.appServerPid, 'SIGKILL') } catch {}
    }
  }
  for (let i = 0; i < 30 && (isService() || serverAlive()); i++) await Bun.sleep(100)
  if (isService() || serverAlive()) throw Error('Backend cleanup unconfirmed')
} else if (verb === 'attach') {
  if (!await healthy()) throw Error('Vision service unavailable; start it first')
  if (!runtime.url || !runtime.threadId) throw Error('Missing remote session identity')
  // Native TUI is a client of the existing app-server, never a separate local resume.
  const p = Bun.spawn([process.env.CODEX_BIN ?? 'codex', 'resume', '--remote', runtime.url, runtime.threadId, '--no-alt-screen'], { cwd: record.path, env: process.env, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' })
  process.exit(await p.exited)
} else throw Error('Unknown lifecycle command')

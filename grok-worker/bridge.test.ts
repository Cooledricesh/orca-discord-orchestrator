import { test, expect, afterEach } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
// Startup guards run before any Discord login, so they are testable offline.
const roots: string[] = []
afterEach(() => { for (const r of roots.splice(0)) rmSync(r, { recursive: true, force: true }) })
function start(reg: Record<string, unknown>) {
  const dir = mkdtempSync(join(tmpdir(), 'grok-bridge-')); roots.push(dir)
  const home = join(dir, 'realhome'), threads = join(dir, 'state/threads'), sd = join(dir, 'sd'), wt = join(dir, 'wt')
  for (const d of [join(home, '.claude'), join(home, '.config'), threads, sd, wt]) mkdirSync(d, { recursive: true })
  writeFileSync(join(dir, 'role.md'), 'rules'); writeFileSync(join(dir, 'prompt.md'), 'start')
  const file = join(threads, '123.json')
  writeFileSync(file, JSON.stringify({ threadId: '123', guildId: 'g', ownerUserId: 'o', bot: '그록1', path: wt, promptFile: join(dir, 'prompt.md'),
    sessionId: 'sid', discordStateDir: sd, rolesFile: join(dir, 'role.md'), ...reg }))
  const p = Bun.spawnSync(['bun', join(import.meta.dir, 'bridge.ts'), file], { env: { PATH: process.env.PATH!, HOME: home, ORCH_ROOT: dir }, stderr: 'pipe' })
  const runtime = JSON.parse(readFileSync(join(threads, '123.grok/runtime.json'), 'utf8'))
  return { p, runtime, iso: join(threads, '123.grok/home') }
}
test('no bot token: failed before login, isolated HOME already built without .claude', () => {
  const { p, runtime, iso } = start({})
  expect(p.exitCode).toBe(1); expect(runtime).toMatchObject({ status: 'failed', sessionStarted: false, sessionId: 'sid' })
  expect(runtime.lastError).toContain('봇 토큰'); expect(readdirSync(iso)).toContain('.config'); expect(existsSync(join(iso, '.claude'))).toBe(false)
})
test('invalid registry and missing role file fail with a reason', () => {
  expect(start({ sessionId: '' }).runtime.lastError).toContain('sessionId')
  expect(start({ rolesFile: '/nonexistent/role.md' }).runtime.lastError).toContain('역할 파일')
})
test('resumed registry starts with --resume (sessionStarted)', () => {
  expect(start({ resumed: true }).runtime.sessionStarted).toBe(true)
})

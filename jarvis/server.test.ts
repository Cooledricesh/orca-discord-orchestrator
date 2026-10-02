import { test, expect } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, lstatSync, chmodSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('dry-run auth failure posts the login notice and ops log, auth.json is a link', () => {
  const d = mkdtempSync(join(tmpdir(), 'jarvis-dry-'))
  try {
    for (const sub of ['bots', 'cwd', 'home', 'state']) mkdirSync(join(d, sub))
    writeFileSync(join(d, 'routes.json'), JSON.stringify({ routes: {}, generalChannelId: 'gen', guildId: 'g', ownerUserId: 'o', bots: { 리뷰어: '자비스' } }))
    const src = join(d, 'src auth', 'auth.json'); mkdirSync(join(d, 'src auth'))
    writeFileSync(src, JSON.stringify({ last_refresh: '2026-10-01T00:00:00Z', tokens: { refresh_token: 'dummy' } }))
    const codex = join(d, 'fake-codex')
    writeFileSync(codex, `#!/bin/sh\ncat >/dev/null\necho '{"type":"error","message":"workspace routing discovery unauthorized (401)"}'\nexit 1\n`)
    chmodSync(codex, 0o755)
    const r = Bun.spawnSync([process.execPath, 'server.ts', '--dry-run', '--message', 'x', '--cwd', join(d, 'cwd')], {
      cwd: import.meta.dir, timeout: 30_000,
      env: {
        PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: join(d, 'home'), ORCH_ROOT: join(import.meta.dir, '..'),
        ROUTES_FILE: join(d, 'routes.json'), STATE_DIR_ROOT: join(d, 'state'), BOTS_DIR: join(d, 'bots'),
        ORCH_CODEX_AUTH_FILE: src, CODEX_BIN: codex,
      },
    })
    const out = r.stdout.toString()
    expect(out).toContain('[post:ops-log]')
    expect(out).toContain('Codex 로그인 문제로 검수에 실패했습니다')
    expect(out).toContain(`CODEX_HOME="${join(d, 'src auth')}" codex login`)
    expect(lstatSync(join(d, 'state', 'jarvis', 'codex-home', 'auth.json')).isSymbolicLink()).toBe(true)
  } finally { rmSync(d, { recursive: true, force: true }) }
}, 40_000)

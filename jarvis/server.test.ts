import { test, expect } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, lstatSync, chmodSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** 임시 루트: routes.json, 더미 원본 인증, 401 을 내고 MARKER 파일을 남기는 가짜 codex (codex 자식 env 는 고정 목록이라 경로를 스크립트에 박는다). 설정 경로는 import 시 env 로 고정되므로 항상 서브프로세스로 돈다. */
function withSandbox(fn: (d: string, env: Record<string, string>) => void) {
  const d = mkdtempSync(join(tmpdir(), 'jarvis-dry-'))
  try {
    for (const sub of ['bots', 'cwd', 'home', 'state', 'src auth']) mkdirSync(join(d, sub))
    writeFileSync(join(d, 'routes.json'), JSON.stringify({ routes: {}, generalChannelId: 'gen', guildId: 'g', ownerUserId: 'o', bots: { 리뷰어: '자비스' } }))
    const src = join(d, 'src auth', 'auth.json')
    writeFileSync(src, JSON.stringify({ last_refresh: '2026-10-01T00:00:00Z', tokens: { refresh_token: 'dummy' } }))
    const codex = join(d, 'fake-codex'), marker = join(d, 'codex-ran')
    writeFileSync(codex, `#!/bin/sh\ntouch '${marker}'\ncat >/dev/null\necho '{"type":"error","message":"workspace routing discovery unauthorized (401)"}'\nexit 1\n`)
    chmodSync(codex, 0o755)
    fn(d, {
      PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: join(d, 'home'), ORCH_ROOT: join(import.meta.dir, '..'),
      ROUTES_FILE: join(d, 'routes.json'), STATE_DIR_ROOT: join(d, 'state'), BOTS_DIR: join(d, 'bots'),
      ORCH_CODEX_AUTH_FILE: src, CODEX_BIN: codex, MARKER: marker, TEST_CWD: join(d, 'cwd'),
    })
  } finally { rmSync(d, { recursive: true, force: true }) }
}

test('dry-run auth failure posts the login notice and ops log, auth.json is a link', () => withSandbox((d, env) => {
  const r = Bun.spawnSync([process.execPath, 'server.ts', '--dry-run', '--message', 'x', '--cwd', env.TEST_CWD!], { cwd: import.meta.dir, timeout: 30_000, env })
  const out = r.stdout.toString()
  expect(out).toContain('[post:ops-log]')
  expect(out).toContain('Codex 로그인 문제로 검수에 실패했습니다')
  expect(out).toContain(`CODEX_HOME="${join(d, 'src auth')}" codex login`)
  expect(lstatSync(join(d, 'state', 'jarvis', 'codex-home', 'auth.json')).isSymbolicLink()).toBe(true)
}), 40_000)

// handle() 한 턴을 가짜 poster 로 돌리고 상태·게시·codex 실행 여부를 RESULT 줄로 낸다
const DRIVER = `
import { existsSync, rmSync } from 'fs'
import { loadConfig, ensureDirs } from './config.ts'
import { prepareCodexHome } from './codex.ts'
import { readState } from './state.ts'
import { Jarvis } from './server.ts'
const cfg = loadConfig(); ensureDirs()
const j = new Jarvis(cfg, prepareCodexHome(cfg), 'self')
const ops = [], posts = []
j.opsLog = async t => { ops.push(t) }
const poster = { postProgress: async () => 'p1', editProgress: async () => {}, postFinal: async (_id, p) => { if (process.env.FAIL_POST) throw new Error('discord down'); posts.push(p.content ?? ''); return ['m1'] } }
if (process.env.DROP_SOURCE) rmSync(process.env.ORCH_CODEX_AUTH_FILE)
let rejected = false
await j.enqueue({ chatId: 'c1', parentChannelId: 'gen', messageId: 'm0', userText: 'x', fetchHistory: async () => [], poster, targetOverride: { cwd: process.env.TEST_CWD, project: 'p', route: null, registry: null } }).catch(() => { rejected = true })
const st = readState('c1')
console.log('RESULT ' + JSON.stringify({ rejected, inflight: st?.inflight ?? null, turns: st?.turns ?? null, posts, ops: ops.length, codexRan: existsSync(process.env.MARKER) }))
`
function drive(env: Record<string, string>): any {
  const r = Bun.spawnSync([process.execPath, '-e', DRIVER], { cwd: import.meta.dir, timeout: 30_000, env })
  const line = r.stdout.toString().split('\n').find(l => l.startsWith('RESULT '))
  if (!line) throw new Error(`no RESULT (exit ${r.exitCode}): ${r.stderr.toString().slice(-500)}`)
  return JSON.parse(line.slice(7))
}

test('failure notice post that throws still saves state with inflight cleared', () => withSandbox((_d, env) => {
  const res = drive({ ...env, FAIL_POST: '1' })
  expect(res).toMatchObject({ rejected: false, inflight: null, turns: 1, ops: 1, codexRan: true })
}), 40_000)

test('auth source gone after startup: codex is not run, login notice and ops log still posted', () => withSandbox((_d, env) => {
  const res = drive({ ...env, DROP_SOURCE: '1' })
  expect(res).toMatchObject({ codexRan: false, inflight: null, ops: 1 })
  expect(res.posts[0]).toContain('Codex 로그인 문제로 검수에 실패했습니다')
}), 40_000)

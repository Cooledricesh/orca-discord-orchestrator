import { test, expect } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readlinkSync, lstatSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { linkAuthFile, isAuthError, authFailureNotice } from './codex.ts'

const auth = (at: string, rt = 'dummy') => JSON.stringify({ last_refresh: at, tokens: { refresh_token: rt } })
function withDir(fn: (d: string, src: string, dst: string) => void) {
  const d = mkdtempSync(join(tmpdir(), 'jarvis-auth-'))
  try { fn(d, join(d, 'src.json'), join(d, 'auth.json')) } finally { rmSync(d, { recursive: true, force: true }) }
}

test('missing dst is linked to source, then stays ok', () => withDir((_d, src, dst) => {
  writeFileSync(src, auth('2026-10-01T00:00:00Z'))
  expect(linkAuthFile(src, dst)).toBe('linked')
  expect(lstatSync(dst).isSymbolicLink()).toBe(true)
  expect(readlinkSync(dst)).toBe(src)
  expect(linkAuthFile(src, dst)).toBe('ok')
}))

test('stale regular copy (same or older last_refresh) is replaced by a link', () => withDir((_d, src, dst) => {
  writeFileSync(src, auth('2026-10-01T00:00:00Z'))
  writeFileSync(dst, auth('2026-10-01T00:00:00Z', 'old'))
  expect(linkAuthFile(src, dst)).toBe('linked')
  expect(readlinkSync(dst)).toBe(src)
  writeFileSync(dst + '2', auth('2026-09-01T00:00:00Z', 'older'))
  expect(linkAuthFile(src, dst + '2')).toBe('linked')
}))

test('in-place write through the link updates the source', () => withDir((_d, src, dst) => {
  writeFileSync(src, auth('2026-10-01T00:00:00Z'))
  linkAuthFile(src, dst)
  writeFileSync(dst, auth('2026-10-02T00:00:00Z', 'rotated'))
  expect(JSON.parse(readFileSync(src, 'utf8')).tokens.refresh_token).toBe('rotated')
  expect(lstatSync(dst).isSymbolicLink()).toBe(true)
}))

test('regular file newer than source is kept unless forced', () => withDir((_d, src, dst) => {
  writeFileSync(src, auth('2026-10-01T00:00:00Z'))
  writeFileSync(dst, auth('2026-10-02T00:00:00Z', 'newer'))
  expect(linkAuthFile(src, dst)).toBe('kept-newer')
  expect(lstatSync(dst).isSymbolicLink()).toBe(false)
  expect(JSON.parse(readFileSync(dst, 'utf8')).tokens.refresh_token).toBe('newer')
  expect(linkAuthFile(src, dst, true)).toBe('linked')
  expect(readlinkSync(dst)).toBe(src)
}))

test('source that is the dst itself stays a regular file', () => withDir(d => {
  const own = join(d, 'home', 'auth.json')
  mkdirSync(join(d, 'home'))
  writeFileSync(own, auth('2026-10-01T00:00:00Z'))
  expect(linkAuthFile(own, join(d, 'home', '.', 'auth.json'))).toBe('ok')
  expect(lstatSync(own).isSymbolicLink()).toBe(false)
}))

test('missing source throws', () => withDir((_d, src, dst) => {
  expect(() => linkAuthFile(src, dst)).toThrow('Codex 인증 파일 없음')
}))

test('auth errors are recognized and the notice names the login command', () => {
  for (const m of [
    'Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.',
    'workspace routing discovery unauthorized (401)',
    'Codex 인증 파일 없음: /x/auth.json — codex login 또는 codexAuthFile 설정',
    'Not logged in',
  ]) expect(isAuthError(m)).toBe(true)
  for (const m of ['시간 초과로 중단', 'codex 종료 코드 1: some stack trace']) expect(isAuthError(m)).toBe(false)
  const n = authFailureNotice('bad\n  token   401', '/Users/me/Application Support/codex/auth.json')
  expect(n).toContain('bad token 401')
  expect(n).toContain('CODEX_HOME="/Users/me/Application Support/codex" codex login')
  expect(n.length).toBeLessThan(1900)
})

import { test, expect, afterEach } from 'bun:test'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readlinkSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { EXCLUDED, buildIsolatedHome } from './home.ts'
const roots: string[] = []
afterEach(() => { for (const r of roots.splice(0)) rmSync(r, { recursive: true, force: true }) })
function fakeHome() {
  const r = mkdtempSync(join(tmpdir(), 'grok-home-')); roots.push(r)
  const home = join(r, 'home'); mkdirSync(home)
  for (const d of ['.claude', '.cursor', '.codex', '.grok', '.config', 'Project']) mkdirSync(join(home, d))
  for (const f of ['.claude.json', '.gitconfig']) writeFileSync(join(home, f), 'x')
  writeFileSync(join(home, 'Project/keep.txt'), 'keep')
  return { r, home, iso: join(r, 'state/threads/1.grok/home') }
}
test('mirrors top-level entries as symlinks, excluding agent configs', () => {
  const { home, iso } = fakeHome(); buildIsolatedHome(home, iso)
  expect(readdirSync(iso).sort()).toEqual(['.config', '.gitconfig', 'Project'])
  for (const e of readdirSync(iso)) { expect(lstatSync(join(iso, e)).isSymbolicLink()).toBe(true); expect(readlinkSync(join(iso, e))).toBe(join(home, e)) }
  for (const e of EXCLUDED) expect(existsSync(join(iso, e))).toBe(false)
  expect(EXCLUDED.sort()).toEqual(['.claude', '.claude.json', '.codex', '.cursor', '.grok'])
})
test('rebuild clears stale entries without following symlinks into the real HOME', () => {
  const { home, iso } = fakeHome(); buildIsolatedHome(home, iso)
  mkdirSync(join(iso, '.claude')); writeFileSync(join(iso, '.claude/settings.json'), '{}')   // created by a tool at runtime
  mkdirSync(join(iso, 'junk')); symlinkSync(join(home, 'Project'), join(iso, 'junk/link'))
  buildIsolatedHome(home, iso)
  expect(existsSync(join(iso, '.claude'))).toBe(false); expect(existsSync(join(iso, 'junk'))).toBe(false)
  expect(existsSync(join(home, 'Project/keep.txt'))).toBe(true); expect(existsSync(join(home, '.claude'))).toBe(true)
})
test('refuses the real HOME or its ancestor', () => {
  const { r, home } = fakeHome()
  expect(() => buildIsolatedHome(home, home)).toThrow(); expect(() => buildIsolatedHome(home, r)).toThrow()
})

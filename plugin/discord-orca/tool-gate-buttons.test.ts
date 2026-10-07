import { afterEach, beforeEach, expect, test } from 'bun:test'
import { mkdtempSync, writeFileSync, readFileSync, rmSync, mkdirSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { GATE_BUTTON, gateApprovalsPath, gateOwner, recordGateClick } from './tool-gate-buttons.ts'

const KEY = 'a'.repeat(64)
let dir: string, path: string
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tool-gate-buttons-'))
  path = join(dir, 'state', 'tool-gate', '123.approvals.json')
  mkdirSync(join(dir, 'state', 'tool-gate'), { recursive: true })
  writeFileSync(path, JSON.stringify({ [KEY]: { messageId: 'm1', command: 'rm -rf x', requested: 1, approved: null } }))
})
afterEach(() => rmSync(dir, { recursive: true, force: true }))

test('custom id format matches the gate', () => {
  expect(GATE_BUTTON.exec(`tgate:allow:${KEY}`)?.slice(1)).toEqual(['allow', KEY])
  expect(GATE_BUTTON.test(`tgate:allow:${KEY.slice(1)}`)).toBe(false)
  expect(GATE_BUTTON.test(`perm:allow:abcde`)).toBe(false)
})

test('paths and owner come from worker env', () => {
  expect(gateApprovalsPath({ ORCA_THREAD_ID: '123', STATE_DIR_ROOT: join(dir, 'state') })).toBe(path)
  expect(gateApprovalsPath({ ORCA_THREAD_ID: '../x', STATE_DIR_ROOT: dir })).toBeNull()
  expect(gateApprovalsPath({ STATE_DIR_ROOT: dir })).toBeNull()
  writeFileSync(join(dir, 'routes.json'), JSON.stringify({ ownerUserId: 42 }))
  expect(gateOwner({ ROUTES_FILE: join(dir, 'routes.json') })).toBe('42')
  expect(gateOwner({ ROUTES_FILE: join(dir, 'none.json') })).toBe('')
})

test('allow records approved seconds on the matching entry', () => {
  expect(recordGateClick(path, KEY, 'm1', 'allow', 1000)).toBe('approved')
  const entry = JSON.parse(readFileSync(path, 'utf8'))[KEY]
  expect(entry.approved).toBe(1000)
  expect(entry.command).toBe('rm -rf x')
})

test('deny records only denied; other message or key is ignored', () => {
  expect(recordGateClick(path, KEY, 'm1', 'deny', 1000)).toBe('denied')
  expect(JSON.parse(readFileSync(path, 'utf8'))[KEY]).toMatchObject({ approved: null, denied: 1000 })
  expect(recordGateClick(path, KEY, 'm2', 'allow')).toBe('missing')
  expect(recordGateClick(path, 'b'.repeat(64), 'm1', 'allow')).toBe('missing')
  expect(recordGateClick(join(dir, 'none.json'), KEY, 'm1', 'allow')).toBe('missing')
  expect(JSON.parse(readFileSync(path, 'utf8'))[KEY].approved).toBeNull()
})

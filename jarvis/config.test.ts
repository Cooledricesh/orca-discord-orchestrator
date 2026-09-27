import { test, expect } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { loadTrustedBots, roleEnabled, botsDirectory, authSource, type Routes } from './config.ts'
import { newState, applyModelRevision } from './state.ts'

test('model revision starts fresh and preserves dedupe and audit state', () => {
  const state = newState('chat', 'project', '/tmp', 'role')
  state.codexThreadId = 'old-session'; state.lastMessageId = 'previous'; state.processed = ['previous']
  expect(applyModelRevision(state, 'new-model')).toBe(true)
  expect(state.codexThreadId).toBeNull()
  expect(state.lastMessageId).toBeNull()
  expect(state.processed).toEqual(['previous'])
  state.codexThreadId = 'new-session'
  expect(applyModelRevision(state, 'new-model')).toBe(false)
  expect(state.codexThreadId).toBe('new-session')
})

test('legacy unpinned reviewer keeps its existing session until explicit change', () => {
  const state = newState('chat', 'project', '/tmp', 'role')
  state.codexThreadId = 'legacy'
  expect(applyModelRevision(state, '')).toBe(false)
  expect(state.codexThreadId).toBe('legacy')
})

test('review trust derives advisor and workers only', () => {
  const dir = mkdtempSync(join(tmpdir(), 'jarvis-trust-'))
  try {
    for (const name of ['advisor', 'mark', 'grok', 'happy']) writeFileSync(join(dir, `${name}.env`), `DISCORD_APP_ID=${name}-id\n`)
    const cfg: Routes = { routes: {}, generalChannelId: 'general', guildId: 'guild', ownerUserId: 'owner', bots: { 상담역: 'advisor', workers: ['mark', { name: 'grok', engine: 'grok' }], 접수원: 'happy' } }
    expect([...loadTrustedBots(cfg, dir).keys()]).toEqual(['advisor-id', 'mark-id', 'grok-id'])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

test('disabled roles are not trusted and vision needs no lounge when disabled', () => {
  const cfg: Routes = { routes: {}, generalChannelId: 'g', guildId: 'guild', ownerUserId: 'o', bots: { 상담역: 'absent', workers: ['absent'] }, enabled: { 상담역: false, 작업자: false, 비전: false } }
  expect([...loadTrustedBots(cfg, '/not-a-bots-folder')]).toEqual([])
  expect(roleEnabled(cfg, '비전')).toBe(false)
})

test('configured paths override the default Discord and Codex locations', () => {
  const cfg: Routes = { routes: {}, generalChannelId: 'g', guildId: 'guild', ownerUserId: 'o', bots: {}, stateRoot: '/custom channels', codexAuthFile: '/chosen account/auth.json' }
  const bots = process.env.BOTS_DIR, auth = process.env.ORCH_CODEX_AUTH_FILE
  try {
    delete process.env.BOTS_DIR; delete process.env.ORCH_CODEX_AUTH_FILE
    expect(botsDirectory(cfg)).toBe('/custom channels/bots')
    expect(authSource(cfg)).toBe('/chosen account/auth.json')
    process.env.BOTS_DIR = '/override bots'; process.env.ORCH_CODEX_AUTH_FILE = '/override auth.json'
    expect(botsDirectory(cfg)).toBe('/override bots')
    expect(authSource(cfg)).toBe('/override auth.json')
  } finally {
    if (bots === undefined) delete process.env.BOTS_DIR; else process.env.BOTS_DIR = bots
    if (auth === undefined) delete process.env.ORCH_CODEX_AUTH_FILE; else process.env.ORCH_CODEX_AUTH_FILE = auth
  }
})

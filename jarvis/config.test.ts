import { test, expect } from 'bun:test'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { loadTrustedBots, type Routes } from './config.ts'

test('review trust derives advisor and workers only', () => {
  const dir = mkdtempSync(join(tmpdir(), 'jarvis-trust-'))
  try {
    for (const name of ['advisor', 'mark', 'happy']) writeFileSync(join(dir, `${name}.env`), `DISCORD_APP_ID=${name}-id\n`)
    const cfg: Routes = { routes: {}, generalChannelId: 'general', guildId: 'guild', ownerUserId: 'owner', bots: { 상담역: 'advisor', workers: ['mark'], 접수원: 'happy' } }
    expect([...loadTrustedBots(cfg, dir).keys()]).toEqual(['advisor-id', 'mark-id'])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

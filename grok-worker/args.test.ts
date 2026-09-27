import { test, expect } from 'bun:test'
import { grokArgs, grokEnv, DISABLED_DISCOVERY } from './args.ts'
const base = { promptFile: '/rt/turn-1.prompt.md', cwd: '/wt', sessionId: 'sid', rules: 'RULES' }
test('new session: --session-id, required flags, no optional ones when empty', () => {
  const a = grokArgs({ ...base, resume: false, model: '', effort: '', sandbox: '' })
  expect(a).toEqual(['--prompt-file', '/rt/turn-1.prompt.md', '--cwd', '/wt', '--session-id', 'sid', '--rules', 'RULES',
    '--output-format', 'streaming-json', '--always-approve', '--no-plan', '--disallowed-tools', 'ask_user_question'])
})
test('resume with model, effort and sandbox', () => {
  const a = grokArgs({ ...base, resume: true, model: 'grok-4.7', effort: 'high', sandbox: 'orch' })
  expect(a.slice(4, 6)).toEqual(['--resume', 'sid']); expect(a).not.toContain('--session-id')
  expect(a.join(' ')).toContain('--model grok-4.7 --reasoning-effort high')
  expect(a.slice(-2)).toEqual(['--sandbox', 'orch'])
})
test('missing essentials throw', () => { expect(() => grokArgs({ ...base, sessionId: '', resume: false })).toThrow() })
test('env: isolated HOME, real GROK_HOME, discovery off, parent kept, bot token stripped', () => {
  const env = grokEnv({ HOME: '/Users/me', ORCH_ROOT: '/o', ORCA_THREAD_ID: '1', DISCORD_BOT_TOKEN: 'x', UNSET: undefined }, '/o/state/threads/1.grok/home', '/Users/me/.grok')
  expect(env.HOME).toBe('/o/state/threads/1.grok/home'); expect(env.GROK_HOME).toBe('/Users/me/.grok')
  expect(env.ORCH_ROOT).toBe('/o'); expect(env.ORCA_THREAD_ID).toBe('1'); expect('DISCORD_BOT_TOKEN' in env).toBe(false); expect('UNSET' in env).toBe(false)
  for (const k of DISABLED_DISCOVERY) expect(env[k]).toBe('false')
  expect(DISABLED_DISCOVERY).toContain('GROK_CLAUDE_MCPS_ENABLED'); expect(DISABLED_DISCOVERY).toContain('GROK_CURSOR_HOOKS_ENABLED')
})
test('env refuses to run without isolation', () => {
  expect(() => grokEnv({ HOME: '/Users/me' }, '/Users/me', '/Users/me/.grok')).toThrow()
  expect(() => grokEnv({ HOME: '/Users/me' }, '', '/Users/me/.grok')).toThrow()
})

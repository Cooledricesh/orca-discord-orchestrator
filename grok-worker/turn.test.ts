import { test, expect, afterEach } from 'bun:test'
import { chmodSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { GrokRun, classify, mask, nextTurn, pruneTurns, patchJSON } from './turn.ts'
import { grokArgs, grokEnv } from './args.ts'
// Offline smoke: a fake GROK_BIN replays the spike fixture, so the turn runner is exercised without Discord or grok.
const roots: string[] = []
afterEach(() => { for (const r of roots.splice(0)) rmSync(r, { recursive: true, force: true }) })
const FIXTURE = join(import.meta.dir, 'fixtures/spike-turn.ndjson')
function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'grok-turn-')); roots.push(dir)
  const bin = join(dir, 'fake-grok')
  writeFileSync(bin, `#!/bin/sh
printf '%s\\n' "$HOME" > "$FAKE_OUT.home"; printf '%s\\n' "$@" > "$FAKE_OUT.args"
[ -n "$FAKE_STDERR" ] && printf '%s\\n' "$FAKE_STDERR" >&2
if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP" & echo $! > "$FAKE_OUT.child"; wait; fi
[ -n "$FAKE_FIXTURE" ] && cat "$FAKE_FIXTURE"
exit \${FAKE_EXIT:-0}
`); chmodSync(bin, 0o755)
  return { dir, bin }
}
function run(fake: Record<string, string>, opts: { noOutputMs?: number } = {}) {
  const { dir, bin } = setup(), tools: string[] = []
  const args = grokArgs({ promptFile: join(dir, 'turn-1.prompt.md'), cwd: '/work/repo', sessionId: 'sid', resume: false, rules: 'R' })
  const env = grokEnv({ PATH: process.env.PATH, HOME: '/real/home', FAKE_OUT: join(dir, 'out'), ...fake }, join(dir, 'home'), '/real/home/.grok')
  const r = new GrokRun({ bin, args, env, cwd: dir, logFile: join(dir, 'turn-1.ndjson'), errFile: join(dir, 'turn-1.err'), noOutputMs: opts.noOutputMs ?? 10_000,
    onEvent: e => { if (e.kind === 'tool') tools.push(e.desc) } })
  // GrokRun parses relative to its cwd; the fixture uses /work/repo, so descriptions keep absolute paths here.
  return { r, dir, tools }
}
test('replays a real turn: events, answer, logs, isolated HOME and argv reach grok', async () => {
  const { r, dir, tools } = run({ FAKE_FIXTURE: FIXTURE })
  const res = await r.result
  expect(res.code).toBe(0); expect(classify(res, false)).toEqual({ kind: 'ok', text: '완료: a.txt의 hello를 hello world로 바꿨습니다.' })
  expect(tools).toEqual(['Read /work/repo/a.txt', '`grep -n hello a.txt`', 'Edit /work/repo/a.txt'])
  expect(readFileSync(join(dir, 'turn-1.ndjson'), 'utf8')).toBe(readFileSync(FIXTURE, 'utf8'))
  expect(readFileSync(join(dir, 'out.home'), 'utf8').trim()).toBe(join(dir, 'home'))
  expect(readFileSync(join(dir, 'out.args'), 'utf8')).toContain('--session-id\nsid\n--rules\nR\n--output-format\nstreaming-json')
})
test('failure modes are classified', async () => {
  const cases: [Record<string, string>, boolean, ReturnType<typeof classify>['kind']][] = [
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'Session "sid" not found locally, restoring conversation from remote… 404 Not Found' }, true, 'resume_missing'],
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'zsh: command not found: foo' }, true, 'failed'],
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'Error: not logged in. Run `grok login`' }, false, 'auth'],
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'HTTP 401 Unauthorized' }, true, 'auth'],
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'session sid already exists' }, false, 'session_exists'],
    [{ FAKE_EXIT: '0', FAKE_STDERR: '' }, false, 'parse'],
    [{ FAKE_EXIT: '1', FAKE_STDERR: 'network down\nfinal: connect ECONNREFUSED' }, false, 'failed'],
  ]
  for (const [fake, resume, kind] of cases) expect(classify(await run(fake).r.result, resume).kind).toBe(kind)
  const failed = classify(await run({ FAKE_EXIT: '1', FAKE_STDERR: 'bad Bearer abc.def and xai-ABCDEFGHijkl1234' }).r.result, false)
  expect(failed).toEqual({ kind: 'failed', detail: 'bad Bearer *** and ***' })
})
test('no output for noOutputMs kills the whole process group', async () => {
  const { r, dir } = run({ FAKE_SLEEP: '30' }, { noOutputMs: 300 })
  const res = await r.result
  expect(res.timedOut).toBe(true); expect(classify(res, false).kind).toBe('timeout')
  const child = Number(readFileSync(join(dir, 'out.child'), 'utf8'))
  await Bun.sleep(100); expect(() => process.kill(child, 0)).toThrow()
})
test('interrupt (중단) signals the group and is reported as interrupted', async () => {
  const { r, dir } = run({ FAKE_SLEEP: '30' })
  await Bun.sleep(300); r.interrupt()
  const res = await r.result
  expect(classify(res, false).kind).toBe('interrupted')
  await r.terminate()   // bridge 중단 path: stragglers (background shell tools ignore SIGINT) are swept with the group
  const child = Number(readFileSync(join(dir, 'out.child'), 'utf8')); await Bun.sleep(100)
  expect(() => process.kill(child, 0)).toThrow()
})
test('turn logs keep the newest five; numbering continues; registry patch is atomic RMW', () => {
  const dir = mkdtempSync(join(tmpdir(), 'grok-prune-')); roots.push(dir)
  for (let n = 1; n <= 8; n++) for (const ext of ['ndjson', 'err', 'prompt.md']) writeFileSync(join(dir, `turn-${n}.${ext}`), '')
  writeFileSync(join(dir, 'runtime.json'), '{}')
  pruneTurns(dir)
  expect(readdirSync(dir).filter(f => f.startsWith('turn-')).map(f => f.split('.')[0]).sort()).toEqual(['turn-4', 'turn-4', 'turn-4', 'turn-5', 'turn-5', 'turn-5', 'turn-6', 'turn-6', 'turn-6', 'turn-7', 'turn-7', 'turn-7', 'turn-8', 'turn-8', 'turn-8'])
  expect(nextTurn(dir)).toBe(9); expect(readdirSync(dir)).toContain('runtime.json')
  const reg = join(dir, 'reg.json'); writeFileSync(reg, JSON.stringify({ status: 'active', sessionId: 'a' }))
  patchJSON(reg, v => { v.previousSessionIds = [v.sessionId]; v.sessionId = 'b' })
  expect(JSON.parse(readFileSync(reg, 'utf8'))).toEqual({ status: 'active', sessionId: 'b', previousSessionIds: ['a'] })
})
test('mask hides token-like runs', () => { expect(mask('Bot abc.def ' + 'A'.repeat(40))).toBe('Bot *** ***') })

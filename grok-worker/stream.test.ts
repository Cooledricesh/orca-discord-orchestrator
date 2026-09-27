import { test, expect } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { TurnStream, describeTool, parseLine } from './stream.ts'
// Real grok 1.0.41 output (spike, isolated HOME), trimmed: thoughts cut, usage signatures and tool outputs dropped, paths → /work/repo.
const fixture = readFileSync(join(import.meta.dir, 'fixtures/spike-turn.ndjson'), 'utf8')
test('spike turn: tools described, pre-tool narration dropped from the answer, end parsed', () => {
  const s = new TurnStream('/work/repo'), tools: string[] = []
  // Feed in odd-sized chunks to exercise line buffering.
  for (let i = 0; i < fixture.length; i += 97) for (const e of s.feed(fixture.slice(i, i + 97))) if (e.kind === 'tool') tools.push(e.desc)
  for (const e of s.flush()) if (e.kind === 'tool') tools.push(e.desc)
  expect(tools).toEqual(['Read a.txt', '`grep -n hello a.txt`', 'Edit a.txt'])
  expect(s.allText).toContain('바꾸겠습니다.')
  expect(s.finalText).toBe('완료: a.txt의 hello를 hello world로 바꿨습니다.')
  expect(s.ok).toBe(true); expect(s.bad).toBe(0); expect(s.tools).toBe(3); expect(s.lastTool).toBe('Edit a.txt')
  expect(s.end).toMatchObject({ sessionId: 'a3edcfd4-f62e-41e5-bb76-0c50d2dda802', stopReason: 'end_turn', cost: 0.02284664, usage: { total_tokens: 62808 } })
})
test('no end event is not ok; non-JSON lines are counted and ignored; error event recorded', () => {
  const lines = fixture.split('\n').filter(l => !l.includes('"type":"end"'))
  const s = new TurnStream('/work/repo'); s.feed(['garbage line', ...lines, '{"no":"type"}', ''].join('\n'))
  expect(s.ok).toBe(false); expect(s.bad).toBe(2); expect(s.finalText).toStartWith('완료:')
  const e = new TurnStream('/w'); e.feed('{"type":"text","data":"hi"}\n{"type":"error","message":"boom"}\n{"type":"end","sessionId":"x","usage":{}}\n')
  expect(e.errors).toEqual(['boom']); expect(e.ok).toBe(false); expect(e.end?.sessionId).toBe('x')
})
test('without tool calls the whole text is the answer; text after the last tool only', () => {
  const s = new TurnStream('/w'); s.feed('{"type":"text","data":"질문"}\n{"type":"text","data":"입니다"}\n'); expect(s.finalText).toBe('질문입니다')
  const t = new TurnStream('/w')
  t.feed(['{"type":"text","data":"먼저 보겠습니다."}', '{"type":"tool_call","toolName":"read_file","rawInput":{"target_file":"/w/a"}}',
    '{"type":"text","data":"중간 서술"}', '{"type":"tool_call","toolName":"write","rawInput":{"file_path":"/w/b"}}', '{"type":"text","data":"\\n최종 답\\n"}', ''].join('\n'))
  expect(t.finalText).toBe('최종 답')
})
test('progress descriptions follow progress-hook.py describe()', () => {
  expect(describeTool('run_terminal_command', { command: 'echo `x`\n  && ls' }, '/w')).toBe("`echo 'x' && ls`")
  expect(describeTool('search_replace', { file_path: '/w/src/a.ts' }, '/w/')).toBe('Edit src/a.ts')
  expect(describeTool('write', { file_path: '/elsewhere/b' }, '/w')).toBe('Write /elsewhere/b')
  expect(describeTool('grep', { pattern: 'foo.*bar' }, '/w')).toBe('Grep foo.*bar')
  expect(describeTool('spawn_subagent', { description: '테스트 실행' }, '/w')).toBe('↳ 서브 에이전트 테스트 실행')
  expect(describeTool('web_search', { query: 'bun test' }, '/w')).toBe('WebSearch bun test')
  expect(describeTool('web_fetch', { url: 'https://x.dev' }, '/w')).toBe('WebFetch https://x.dev')
  expect(describeTool('todo_write', {}, '/w')).toBe('todo_write')
  expect(parseLine('   ', '/w')).toBeUndefined()
  expect(parseLine('{"type":"available_commands","tools":[]}', '/w')).toEqual({ kind: 'other', type: 'available_commands' })
})

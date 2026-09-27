/** grok `--output-format streaming-json` parser (pure). One ACP session update per line, `type` field on each.
 * Seen (1.0.41): available_commands, thought, text, tool_call, tool_call_update, usage, end. Unknown types are ignored. */
export type Usage = { input_tokens?: number; cache_read_input_tokens?: number; output_tokens?: number; reasoning_tokens?: number; total_tokens?: number }
export type End = { sessionId: string; stopReason: string; usage: Usage; cost?: number; numTurns?: number }
export type Ev =
  | { kind: 'tool'; name: string; desc: string }
  | { kind: 'text'; text: string }
  | { kind: 'end'; end: End }
  | { kind: 'error'; message: string }
  | { kind: 'bad'; line: string }
  | { kind: 'other'; type: string }
const str = (v: unknown) => typeof v === 'string' ? v : ''
const squash = (s: string) => s.replace(/\s+/g, ' ').trim()
/** Progress line in the style of bin/progress-hook.py describe(): what is being touched, cwd-relative paths. */
export function describeTool(name: string, input: any, cwd: string): string {
  const i = input && typeof input === 'object' ? input : {}
  const rel = (p: string) => cwd && p.startsWith(cwd.replace(/\/$/, '') + '/') ? p.slice(cwd.replace(/\/$/, '').length + 1) : p
  const path = () => rel(str(i.file_path) || str(i.target_file) || str(i.path) || str(i.target_directory) || str(i.directory))
  switch (name) {
    case 'run_terminal_command': return squash('`' + str(i.command).replace(/`/g, "'") + '`')
    case 'read_file': return squash('Read ' + path())
    case 'search_replace': case 'edit_file': return squash('Edit ' + path())
    case 'write': case 'write_file': return squash('Write ' + path())
    case 'list_dir': return squash('List ' + path())
    case 'grep': return squash('Grep ' + (str(i.pattern) || str(i.query)))
    case 'spawn_subagent': return squash('↳ 서브 에이전트 ' + (str(i.description) || str(i.task) || str(i.prompt)))
    case 'web_search': return squash('WebSearch ' + (str(i.query) || str(i.url)))
    case 'web_fetch': return squash('WebFetch ' + (str(i.url) || str(i.query)))
    default: return name || 'tool'
  }
}
export function parseLine(line: string, cwd: string): Ev | undefined {
  if (!line.trim()) return undefined
  let v: any
  try { v = JSON.parse(line) } catch { return { kind: 'bad', line } }
  if (!v || typeof v !== 'object' || typeof v.type !== 'string') return { kind: 'bad', line }
  switch (v.type) {
    case 'tool_call': { const name = str(v.toolName) || str(v.title); return { kind: 'tool', name, desc: describeTool(name, v.rawInput, cwd) } }
    case 'text': return { kind: 'text', text: str(v.data) }
    case 'end': return { kind: 'end', end: { sessionId: str(v.sessionId), stopReason: str(v.stopReason), usage: v.usage ?? {},
      cost: typeof v.total_cost_usd === 'number' ? v.total_cost_usd : undefined, numTurns: typeof v.num_turns === 'number' ? v.num_turns : undefined } }
    case 'error': return { kind: 'error', message: str(v.message) || str(v.error?.message) || str(v.error) || JSON.stringify(v).slice(0, 300) }
    default: return { kind: 'other', type: v.type }
  }
}
/** Accumulates one turn. Text before the last tool_call is narration ("…하겠습니다.") and is dropped from the answer. */
export class TurnStream {
  tools = 0; bad = 0; lastTool = ''; end?: End; errors: string[] = []; events = 0
  private text = ''; private afterTool = ''; private buf = ''
  constructor(private cwd: string) {}
  /** Raw stdout chunk → complete-line events. */
  feed(chunk: string): Ev[] {
    this.buf += chunk; const lines = this.buf.split('\n'); this.buf = lines.pop()!
    return lines.flatMap(l => { const e = this.line(l); return e ? [e] : [] })
  }
  flush(): Ev[] { const l = this.buf; this.buf = ''; const e = this.line(l); return e ? [e] : [] }
  line(l: string): Ev | undefined {
    const e = parseLine(l, this.cwd); if (!e) return undefined
    if (e.kind !== 'bad' && !(e.kind === 'other' && e.type === 'available_commands')) this.events++
    if (e.kind === 'bad') this.bad++
    else if (e.kind === 'tool') { this.tools++; this.lastTool = e.desc; this.afterTool = '' }
    else if (e.kind === 'text') { this.text += e.text; this.afterTool += e.text }
    else if (e.kind === 'end') this.end = e.end
    else if (e.kind === 'error') this.errors.push(e.message)
    return e
  }
  /** The user-facing answer: text after the last tool_call (all text when no tool ran). */
  get finalText() { return this.afterTool.trim() }
  get allText() { return this.text.trim() }
  /** Parsed cleanly: an end event and no error event. */
  get ok() { return !!this.end && !this.errors.length }
}

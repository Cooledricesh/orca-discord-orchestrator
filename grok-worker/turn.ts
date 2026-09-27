/** One grok turn = one process (detached group), NDJSON on stdout. Discord-free so it runs under bun test with a fake GROK_BIN. */
import { spawn, type ChildProcess } from 'node:child_process'
import { createWriteStream, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { TurnStream, type Ev } from './stream.ts'
export type TurnResult = { code: number | null; signal: string | null; stream: TurnStream; stderr: string; timedOut: boolean; interrupted: boolean; ms: number }
export type RunOpts = { bin: string; args: string[]; env: Record<string, string>; cwd: string; logFile: string; errFile: string; noOutputMs: number; onEvent?: (e: Ev) => void; onOutput?: () => void }
export class GrokRun {
  child: ChildProcess; result: Promise<TurnResult>; interrupted = false; timedOut = false; started = Date.now()
  constructor(o: RunOpts) {
    const stream = new TurnStream(o.cwd), log = createWriteStream(o.logFile, { mode: 0o600 }), err = createWriteStream(o.errFile, { mode: 0o600 })
    let stderr = '', idle: ReturnType<typeof setTimeout> | undefined
    const alive = () => { if (idle) clearTimeout(idle); idle = setTimeout(() => { this.timedOut = true; void this.terminate(5000) }, o.noOutputMs); o.onOutput?.() }
    this.child = spawn(o.bin, o.args, { cwd: o.cwd, env: o.env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })
    const emit = (evs: Ev[]) => { for (const e of evs) try { o.onEvent?.(e) } catch {} }
    this.child.stdout!.setEncoding('utf8').on('data', (d: string) => { log.write(d); alive(); emit(stream.feed(d)) })
    this.child.stderr!.setEncoding('utf8').on('data', (d: string) => { err.write(d); alive(); stderr = (stderr + d).slice(-65536) })
    alive()
    this.result = new Promise(res => {
      let done = false
      const finish = (code: number | null, signal: string | null) => {
        if (done) return; done = true; if (idle) clearTimeout(idle); emit(stream.flush())
        const close = (s: NodeJS.WritableStream) => new Promise<void>(r => s.end(() => r()))
        void Promise.all([close(log), close(err)]).then(() => res({ code, signal, stream, stderr, timedOut: this.timedOut, interrupted: this.interrupted, ms: Date.now() - this.started }))
      }
      this.child.on('error', e => { stderr += `\n${e.message}`; finish(127, null) })
      // 'close' waits for stdio; a leaked grandchild holding the pipe must not stall the turn after grok itself exited.
      this.child.on('exit', (code, signal) => { setTimeout(() => finish(code, signal), 2000) })
      this.child.on('close', (code, signal) => finish(code, signal))
    })
  }
  /** Signals the whole grok process group (its shell tools and subagents included). */
  kill(sig: NodeJS.Signals) { const pid = this.child.pid; if (!pid) return; try { process.kill(-pid, sig) } catch { try { this.child.kill(sig) } catch {} } }
  interrupt() { this.interrupted = true; this.kill('SIGINT') }
  /** TERM, then KILL after graceMs if the group is still there. */
  async terminate(graceMs = 1500) {
    this.kill('SIGTERM')
    await Promise.race([this.result, Bun.sleep(graceMs)])
    this.kill('SIGKILL')
  }
}
export type Outcome =
  | { kind: 'ok'; text: string }
  | { kind: 'parse'; text: string }
  | { kind: 'interrupted' }
  | { kind: 'timeout'; text: string }
  | { kind: 'resume_missing' }
  | { kind: 'session_exists' }
  | { kind: 'auth' }
  | { kind: 'failed'; detail: string }
const AUTH = /\b401\b|unauthori[sz]ed|not (?:logged|signed) in|(?:log|sign) ?in (?:again|required|expired)|grok login|authenticat|(?:token|session|credentials?) (?:has |have |is )?(?:expired|invalid)/i
export const exitLabel = (r: Pick<TurnResult, 'code' | 'signal'>) => String(r.code ?? r.signal ?? '?')
/** Token-like runs are masked before anything from stderr reaches Discord. */
export const mask = (s: string) => s.replace(/\b(Bearer|Bot)\s+\S+/gi, '$1 ***').replace(/\b(?:xai|sk)-[A-Za-z0-9_-]{8,}/g, '***').replace(/[A-Za-z0-9+_=-]{32,}/g, '***')
export const lastLine = (s: string) => s.split('\n').map(l => l.trim()).filter(Boolean).pop() ?? ''
export function classify(r: TurnResult, resume: boolean): Outcome {
  if (r.interrupted) return { kind: 'interrupted' }
  if (r.timedOut) return { kind: 'timeout', text: r.stream.finalText }
  if (r.code === 0) return r.stream.ok ? { kind: 'ok', text: r.stream.finalText } : { kind: 'parse', text: r.stream.finalText }
  const errText = r.stderr + '\n' + r.stream.errors.join('\n')
  if (resume && /session\b[^\n]*not found|not found locally/i.test(errText)) return { kind: 'resume_missing' }
  if (!resume && /already exists|in use/i.test(errText)) return { kind: 'session_exists' }
  if (AUTH.test(errText)) return { kind: 'auth' }
  return { kind: 'failed', detail: mask(lastLine(r.stderr) || lastLine(r.stream.errors.join('\n'))).slice(0, 300) }
}
/** turn-<n>.{ndjson,err,prompt.md}: keep the newest `keep` turns. */
export function pruneTurns(dir: string, keep = 5) {
  const files = readdirSync(dir).map(f => [f, /^turn-(\d+)\./.exec(f)?.[1]] as const).filter(([, n]) => n)
  const nums = [...new Set(files.map(([, n]) => Number(n)))].sort((a, b) => b - a).slice(keep)
  for (const [f, n] of files) if (nums.includes(Number(n))) try { unlinkSync(join(dir, f)) } catch {}
}
export function nextTurn(dir: string) { let max = 0; for (const f of readdirSync(dir)) { const n = Number(/^turn-(\d+)\./.exec(f)?.[1] ?? 0); if (n > max) max = n }; return max + 1 }
/** Atomic read-modify-write of a JSON file (tmp + rename), same shape as lib.sh registry_write. */
export function patchJSON(file: string, fn: (v: any) => void) {
  const v = JSON.parse(readFileSync(file, 'utf8')); fn(v)
  const tmp = `${file}.${process.pid}.tmp`; writeFileSync(tmp, JSON.stringify(v, null, 2) + '\n', { mode: 0o600 }); renameSync(tmp, file)
}
export function writeJSONAtomic(file: string, v: unknown) {
  const tmp = `${file}.${process.pid}.tmp`; writeFileSync(tmp, JSON.stringify(v, null, 2) + '\n', { mode: 0o600 }); renameSync(tmp, file)
}

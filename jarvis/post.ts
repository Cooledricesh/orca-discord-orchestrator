// 게시: 진행 메시지(5초 간격 edit) → 최종 답변. 길면 요약 embed + 전문 파일 첨부.
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'
import { AttachmentBuilder, EmbedBuilder, type Message, type TextBasedChannel } from 'discord.js'
import { RUNS_DIR } from './config.ts'

export const MAX_INLINE = 1900
const PROGRESS_INTERVAL_MS = 5000
const EMBED_SUMMARY_CHARS = 1000

export interface Poster {
  /** 진행 메시지 생성. 반환값은 message id (dry-run 은 가짜). */
  postProgress(text: string): Promise<string>
  editProgress(id: string, text: string): Promise<void>
  /** 최종 답변. progressId 가 있으면 그 메시지를 교체(edit), 없으면 새 메시지. 반환은 게시된 message id 들. */
  postFinal(progressId: string | null, payload: FinalPayload): Promise<string[]>
}

export interface FinalPayload { content?: string; embed?: { title: string; description: string; color?: number }; filePath?: string; fileName?: string }

/** 최종 텍스트 → 게시 형태. 1900자 이하면 본문 하나, 넘으면 embed 요약 + 파일. */
export function buildFinal(text: string, opt: { project: string; chatId: string; turn: number; color?: number }): FinalPayload {
  const body = text.trim() || '(빈 응답)'
  if (body.length <= MAX_INLINE) return { content: body }
  const summary = extractSummary(body, EMBED_SUMMARY_CHARS)
  const day = new Date().toISOString().slice(0, 10)
  const dir = join(RUNS_DIR, opt.project)
  mkdirSync(dir, { recursive: true })
  const fileName = `jarvis-${day}-${opt.chatId}-${opt.turn}.md`
  const filePath = join(dir, fileName)
  writeFileSync(filePath, `# 자비스 검토 ${day} (${opt.project}, thread ${opt.chatId}, turn ${opt.turn})\n\n${body}\n`)
  return {
    embed: { title: `검토 결과 (전문 ${body.length}자 — 첨부 파일)`, description: summary, color: opt.color },
    filePath, fileName,
  }
}

/** 모델이 '## 요약'/'요약:' 섹션을 냈으면 그것을, 아니면 앞부분을 요약으로. */
export function extractSummary(body: string, max: number): string {
  const m = body.match(/(?:^|\n)#{1,3}\s*(?:요약|판정|Summary)[^\n]*\n([\s\S]*?)(?=\n#{1,3}\s|\s*$)/i)
  let s = m?.[1]?.trim() || body
  if (s.length > max) s = s.slice(0, max - 1) + '…'
  return s
}

/** 진행 표시 스로틀: 5초에 최대 1회 edit. */
export class ProgressReporter {
  private lastEdit = 0
  private pending: string | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private id: string | null = null
  private closed = false
  constructor(private poster: Poster, private label: string) {}
  async start(): Promise<string> { this.id = await this.poster.postProgress(`${this.label} 검토 중…`); return this.id }
  update(summary: string) {
    if (this.closed || !this.id) return
    this.pending = `${this.label} 검토 중… ${summary}`.slice(0, MAX_INLINE)
    const wait = PROGRESS_INTERVAL_MS - (Date.now() - this.lastEdit)
    if (this.timer) return
    this.timer = setTimeout(() => { this.timer = null; void this.flush() }, Math.max(0, wait))
  }
  private async flush() {
    if (this.closed || !this.id || this.pending == null) return
    const t = this.pending; this.pending = null; this.lastEdit = Date.now()
    try { await this.poster.editProgress(this.id, t) } catch {}
  }
  close() { this.closed = true; if (this.timer) { clearTimeout(this.timer); this.timer = null } }
}

export function summarizeProgress(kind: string, text: string): string {
  const one = text.replace(/\s+/g, ' ').trim()
  const short = one.length > 120 ? one.slice(0, 119) + '…' : one
  if (kind === 'command') return `\`${short.replace(/`/g, "'")}\``
  return short
}

// --- Discord 구현 ----------------------------------------------------------
export class DiscordPoster implements Poster {
  constructor(private channel: TextBasedChannel) {}
  private get sendable() { return this.channel as unknown as { send: (o: any) => Promise<Message>; messages: { fetch: (id: string) => Promise<Message> } } }
  async postProgress(text: string) { const m = await this.sendable.send({ content: text, allowedMentions: { parse: [] } }); return m.id }
  async editProgress(id: string, text: string) { const m = await this.sendable.messages.fetch(id); await m.edit({ content: text, allowedMentions: { parse: [] } }) }
  async postFinal(progressId: string | null, p: FinalPayload) {
    const opts: any = { content: p.content ?? '', allowedMentions: { parse: [] }, embeds: [], files: [] }
    if (p.embed) opts.embeds = [new EmbedBuilder().setTitle(p.embed.title).setDescription(p.embed.description).setColor(p.embed.color ?? 0x5865f2)]
    if (p.filePath) opts.files = [new AttachmentBuilder(p.filePath, { name: p.fileName })]
    if (progressId) {
      try { const m = await this.sendable.messages.fetch(progressId); await m.edit(opts); return [m.id] } catch {}
    }
    const m = await this.sendable.send(opts)
    return [m.id]
  }
}

// --- dry-run 구현 (stdout) -------------------------------------------------
export class ConsolePoster implements Poster {
  private n = 0
  private out(kind: string, s: string) { process.stdout.write(`\n=== [post:${kind}] ===\n${s}\n`) }
  async postProgress(text: string) { this.n++; this.out('progress', text); return `dry-${this.n}` }
  async editProgress(id: string, text: string) { this.out(`progress-edit ${id}`, text) }
  async postFinal(progressId: string | null, p: FinalPayload) {
    const parts: string[] = []
    if (p.content) parts.push(p.content)
    if (p.embed) parts.push(`[embed] ${p.embed.title}\n${p.embed.description}`)
    if (p.filePath) parts.push(`[attachment] ${p.filePath}`)
    this.out(`final${progressId ? ` (replaces ${progressId})` : ''}`, parts.join('\n'))
    return ['dry-final']
  }
}

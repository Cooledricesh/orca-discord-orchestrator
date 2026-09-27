/** Discord I/O shared by the bridges (vision, grok). No discord.js import: callers pass their channel objects, so this
 * module resolves from any package without its own node_modules. */
import { assertPublicAttachment } from '../plugin/discord-orca/file-policy.ts'
import { mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
export const MAX_FILE = 25 * 1024 * 1024
export const CHUNK = 1900
const CDN_HOSTS = ['cdn.discordapp.com', 'media.discordapp.net']
/** DISCORD_BOT_TOKEN from <stateDir>/.env. The value is never logged. */
export function readBotToken(stateDir: string): string {
  const env = readFileSync(join(stateDir, '.env'), 'utf8')
  const token = /^DISCORD_BOT_TOKEN=(.*)$/m.exec(env)?.[1]?.trim().replace(/^['"]|['"]$/g, '')
  if (!token) throw Error('Bot token missing')
  return token
}
/** Outgoing attachments: only the own inbox or non-private regular files, at most 25MB. */
export function assertFile(f: string, stateDir: string, privateRoots?: string[]) {
  assertPublicAttachment(f, stateDir, privateRoots)
  const real = realpathSync(f)
  if (!statSync(real).isFile() || statSync(real).size > MAX_FILE) throw Error('File must be regular and at most 25MB')
}
export type AttachmentLike = { id: string; name?: string | null; size: number; url: string }
/** Downloads to <inbox>/<id>-<safe name>. Discord CDN over https only, no redirects, 25MB cap enforced while streaming. */
export async function downloadAttachments(attachments: Iterable<AttachmentLike>, inbox: string): Promise<string[]> {
  mkdirSync(inbox, { recursive: true, mode: 0o700 }); const paths: string[] = []
  for (const att of attachments) paths.push(await downloadAttachment(att, inbox))
  return paths
}
export async function downloadAttachment(att: AttachmentLike, inbox: string): Promise<string> {
  if (att.size > MAX_FILE) throw Error('Attachment exceeds 25MB')
  const url = new URL(att.url)
  if (url.protocol !== 'https:' || !CDN_HOSTS.includes(url.hostname)) throw Error('Unexpected attachment host')
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(30000) })
  if (!response.ok) throw Error('Attachment download failed')
  const reader = response.body!.getReader(); let size = 0; const data: Uint8Array[] = []
  while (true) { const r = await reader.read(); if (r.done) break; size += r.value.length; if (size > MAX_FILE) { await reader.cancel(); throw Error('Attachment exceeds 25MB') }; data.push(r.value) }
  mkdirSync(inbox, { recursive: true, mode: 0o700 })
  const path = join(inbox, `${att.id}-${basename(att.name ?? 'attachment').replace(/[^\p{L}\p{N}._-]/gu, '_')}`)
  writeFileSync(path, Buffer.concat(data), { mode: 0o600 }); return path
}
export const chunks = (text: string) => text.match(/[\s\S]{1,1900}/g) ?? ['']
export type Sendable = { send(options: any): Promise<{ id: string }> }
/** 1900-char chunks; files and the reply reference ride on the first chunk only. */
export async function sendChunked(ch: Sendable, text: string, opts: { users: string[]; files?: string[]; replyTo?: string }): Promise<string[]> {
  const ids: string[] = [], parts = chunks(text)
  for (let i = 0; i < parts.length; i++) {
    const msg = await ch.send({ content: parts[i], ...(i === 0 && opts.files ? { files: opts.files } : {}),
      ...(i === 0 && opts.replyTo ? { reply: { messageReference: opts.replyTo, failIfNotExists: false } } : {}),
      allowedMentions: { parse: [], users: opts.users },
    }); ids.push(msg.id)
  }
  return ids
}

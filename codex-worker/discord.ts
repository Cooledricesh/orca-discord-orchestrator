import { Client, GatewayIntentBits, type Message } from 'discord.js'
import { join } from 'node:path'
import { assertFile, downloadAttachments, readBotToken, sendChunked } from '../bridge-kit/discord-io.ts'
import { mentionsOtherBotOnly } from '../plugin/discord-orca/gate-helpers.ts'
type DynamicToolSpec = { type: 'function'; name: string; description: string; inputSchema: Record<string, unknown>; deferLoading: boolean }
/** Vision lounge: one channel plus its threads, one Codex session, owner messages only. */
const definitions: [string, string, Record<string, any>, string[]][] = [
  ['reply', 'Send the user-facing reply to Discord. The terminal transcript is not mirrored.', { text: { type: 'string' }, reply_to: { type: 'string' }, files: { type: 'array', items: { type: 'string' }, maxItems: 10 } }, ['text']],
  ['create_thread', 'Create a result thread in the lounge only when the user asks for one.', { name: { type: 'string' } }, ['name']],
  ['fetch_messages', 'Fetch Discord history, oldest first. Only when needed.', { limit: { type: 'integer', minimum: 1, maximum: 100 }, before: { type: 'string' } }, []],
  ['download_attachment', 'Download message attachments to the local inbox.', { message_id: { type: 'string' } }, ['message_id']],
]
export const toolSpecs: DynamicToolSpec[] = definitions.map(([name, description, properties, required]) => ({
  type: 'function' as const, name: `discord_${name}`, description, deferLoading: false,
  inputSchema: { type: 'object', additionalProperties: false, required: [...required, 'chat_id'],
    properties: { ...properties, chat_id: { type: 'string', description: 'The chat_id the input came from (lounge or one of its threads).' } } },
}))
export type DiscordConfig = { channel: string; guild: string; owner: string; stateDir: string; privateRoots?: string[] }
export class DiscordPort {
  client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent] })
  constructor(readonly cfg: DiscordConfig) {}
  token(): string { return readBotToken(this.cfg.stateDir) }
  accepts(m: Message): boolean {
    const inScope = m.channelId === this.cfg.channel || (m.channel?.isThread() && m.channel.parentId === this.cfg.channel)
    if (m.guildId !== this.cfg.guild || !inScope || m.webhookId || m.author.bot || m.author.id !== this.cfg.owner) return false
    return !mentionsOtherBotOnly([...m.mentions.parsedUsers.values()].map(u => ({ id: u.id, bot: u.bot })), this.client.user?.id)
  }
  async channel(id: string): Promise<any> {
    const ch = await this.client.channels.fetch(id)
    if (!ch || !('guildId' in ch) || ch.guildId !== this.cfg.guild) throw Error('Channel outside guild')
    if ((ch.isThread() ? ch.parentId : ch.id) !== this.cfg.channel) throw Error('Only the lounge and its threads are allowed')
    if (!('messages' in ch)) throw Error('Channel has no messages')
    return ch
  }
  async send(text: string, id = this.cfg.channel) {
    return (await this.channel(id)).send({ content: text, allowedMentions: { parse: [], users: [this.cfg.owner] } })
  }
  async tool(name: string, a: any): Promise<string> {
    if (typeof a?.chat_id !== 'string') throw Error('chat_id required: reply where the input came from')
    const ch = await this.channel(a.chat_id)
    switch (name) {
      case 'discord_reply': {
        if (typeof a.text !== 'string') throw Error('text required')
        const files: string[] = a.files ?? []
        if (!Array.isArray(files) || files.length > 10) throw Error('Max 10 files')
        for (const f of files) this.assertFile(f)
        const ids = await sendChunked(ch, a.text, { users: [this.cfg.owner], files, replyTo: a.reply_to })
        return `sent IDs: ${ids.join(', ')}`
      }
      case 'discord_create_thread': {
        if (ch.id !== this.cfg.channel || typeof a.name !== 'string' || !a.name.trim()) throw Error('Result threads are created from the lounge channel with a name')
        const thread = await ch.threads.create({ name: a.name.slice(0, 100), autoArchiveDuration: 1440 })
        return JSON.stringify({ chat_id: thread.id })
      }
      case 'discord_fetch_messages': {
        const msgs = await ch.messages.fetch({ limit: Math.max(1, Math.min(Number(a.limit) || 20, 100)), ...(a.before ? { before: a.before } : {}) })
        return JSON.stringify([...msgs.values()].reverse().map((m: any) => ({ id: m.id, author: m.author.id, bot: m.author.bot, text: m.content, attachments: [...m.attachments.values()].map((x: any) => ({ name: x.name, size: x.size })) })))
      }
      case 'discord_download_attachment': {
        const msg = await ch.messages.fetch(a.message_id)
        const paths = await downloadAttachments(msg.attachments.values(), join(this.cfg.stateDir, 'inbox'))
        return JSON.stringify(paths)
      }
      default: throw Error('Unknown Discord tool')
    }
  }
  assertFile(f: string) { assertFile(f, this.cfg.stateDir, this.cfg.privateRoots) }
}

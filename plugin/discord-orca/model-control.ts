import { readFileSync } from 'fs'
import { join } from 'path'
import { spawn } from 'child_process'
import { ChannelType, type Message } from 'discord.js'

/** Intercept only explicit owner commands; ordinary chat still goes to Claude. */
export function isModelCommand(content: string): boolean {
  return /^\s*!(?:모델|model)(?:\s|$)/.test(content)
}

export async function handleModelCommand(msg: Message): Promise<boolean> {
  const root = process.env.ORCH_ROOT
  if (process.env.ORCA_ROLE !== '상담역' || !root || !isModelCommand(msg.content)) return false
  const reply = (content: string) => msg.reply({ content, allowedMentions: { parse: [], repliedUser: false } })
  try {
    const routes = JSON.parse(readFileSync(process.env.ROUTES_FILE ?? join(root, 'routes.json'), 'utf8'))
    if (msg.author.bot || msg.author.id !== routes.ownerUserId) return true
    if (msg.channel.type !== ChannelType.DM) {
      await reply('모델 관리는 프라이데이 1:1 DM에서만 가능합니다.')
      return true
    }
    const script = join(root, 'bin/model_control.py')
    const p = Bun.spawn(['python3', script], { stdin: 'pipe', stdout: 'pipe', stderr: 'ignore', timeout: 8000 })
    p.stdin.write(JSON.stringify({ content: msg.content, authorId: msg.author.id,
      authorIsBot: msg.author.bot, isDM: true, channelId: msg.channelId }))
    p.stdin.end()
    const output = await new Response(p.stdout).text()
    if (await p.exited !== 0) throw new Error('model-control failed')
    const result = JSON.parse(output)
    await reply(result.text)
    if (result.job && /^[a-f0-9]{8}$/.test(result.job)) {
      // Detached from the MCP/Claude process group, so Friday may restart itself.
      const child = spawn('python3', [script, '--apply', result.job], { detached: true, stdio: 'ignore' })
      child.on('error', () => { void reply('적용 프로세스를 시작하지 못했습니다. !모델로 상태를 확인하세요.').catch(() => {}) })
      child.unref()
    }
  } catch {
    await reply('모델 관리 명령 처리에 실패했습니다. Claude에 전달하지 않았습니다.').catch(() => {})
  }
  return true
}

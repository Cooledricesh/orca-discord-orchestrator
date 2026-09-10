// 게이트: 메시지를 처리할지 결정하는 유일한 함수. 순수 함수라 테스트 가능.
export interface GateInput {
  messageId: string
  authorId: string
  authorIsBot: boolean
  /** 우리 봇(프라이데이·마크) 이면 true — 소유자 대신 브리프를 보낼 수 있다 */
  authorIsTrustedBot?: boolean
  guildId: string | null
  /** 메시지가 올라온 채널 (스레드면 스레드 id) */
  channelId: string
  /** 스레드면 부모 채널 id, 아니면 null */
  parentId: string | null
  /** msg.mentions.users 의 id 목록 */
  mentionedUserIds: string[]
}

export interface GateCfg {
  ownerUserId: string
  guildId: string
  /** routes 채널 + generalChannelId */
  allowedChannelIds: Set<string>
  selfAppId: string
  /** 이미 처리한 message id 인지 */
  isProcessed: (chatId: string, messageId: string) => boolean
  /** 봇 작성자 속도 제한 (없으면 무제한) */
  rateGuard?: RateGuard
}

export type GateResult = { action: 'deliver'; chatId: string; parentChannelId: string } | { action: 'drop'; reason: string }

export function gate(m: GateInput, cfg: GateCfg): GateResult {
  const isOwner = m.authorId === cfg.ownerUserId && !m.authorIsBot
  const isTrustedBot = m.authorIsBot && !!m.authorIsTrustedBot
  if (m.authorIsBot && !isTrustedBot) return { action: 'drop', reason: 'untrusted bot author' }
  if (!isOwner && !isTrustedBot) return { action: 'drop', reason: 'not owner' }
  if (m.guildId !== cfg.guildId) return { action: 'drop', reason: 'other guild' }
  const parent = m.parentId ?? m.channelId
  if (!cfg.allowedChannelIds.has(parent)) return { action: 'drop', reason: 'channel not routed' }
  if (!m.mentionedUserIds.includes(cfg.selfAppId)) return { action: 'drop', reason: 'no mention' }
  if (cfg.isProcessed(m.channelId, m.messageId)) return { action: 'drop', reason: 'already processed' }
  if (isTrustedBot && cfg.rateGuard && !cfg.rateGuard.allow(`${m.authorId}:${m.channelId}`)) return { action: 'drop', reason: 'bot rate limit' }
  return { action: 'deliver', chatId: m.channelId, parentChannelId: parent }
}

/** 키별 속도 제한: 창(windowMs) 안에 max 회를 넘기면 cooldownMs 동안 거부. 소유자에게는 쓰지 않는다. */
export class RateGuard {
  private hits = new Map<string, number[]>()
  private blockedUntil = new Map<string, number>()
  constructor(private max = 3, private windowMs = 60_000, private cooldownMs = 60_000, private now: () => number = Date.now) {}
  allow(key: string): boolean {
    const t = this.now()
    const until = this.blockedUntil.get(key) ?? 0
    if (t < until) return false
    const arr = (this.hits.get(key) ?? []).filter(x => t - x < this.windowMs)
    arr.push(t); this.hits.set(key, arr)
    if (arr.length > this.max) { this.blockedUntil.set(key, t + this.cooldownMs); this.hits.set(key, []); return false }
    return true
  }
}

/** 본문에서 자비스 멘션(<@id>, <@!id>)을 지우고 앞뒤 공백 정리. */
export function stripSelfMention(content: string, selfAppId: string): string {
  return content.replace(new RegExp(`<@!?${selfAppId}>`, 'g'), '').replace(/[ \t]{2,}/g, ' ').trim()
}

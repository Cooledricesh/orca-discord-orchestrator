/** grok headless argv/env (pure). Contract §4 / design §4.1. The prompt always goes through --prompt-file so text that
 * starts with `-` or is very long is never parsed as a flag. */
export type TurnSpec = { promptFile: string; cwd: string; sessionId: string; resume: boolean; rules: string; model?: string; effort?: string; sandbox?: string }
export function grokArgs(s: TurnSpec): string[] {
  if (!s.promptFile || !s.cwd || !s.sessionId) throw Error('promptFile, cwd and sessionId are required')
  return ['--prompt-file', s.promptFile, '--cwd', s.cwd, s.resume ? '--resume' : '--session-id', s.sessionId,
    '--rules', s.rules, '--output-format', 'streaming-json', '--always-approve',
    ...(s.model ? ['--model', s.model] : []), ...(s.effort ? ['--reasoning-effort', s.effort] : []),
    '--no-plan', '--disallowed-tools', 'ask_user_question', ...(s.sandbox ? ['--sandbox', s.sandbox] : [])]
}
export const DISABLED_DISCOVERY = ['SKILLS', 'RULES', 'AGENTS', 'MCPS', 'HOOKS'].flatMap(k => [`GROK_CLAUDE_${k}_ENABLED`, `GROK_CURSOR_${k}_ENABLED`])
/** Parent env (runtime_exports, ORCA_THREAD_ID …) + isolated HOME. Refuses to build without isolation. */
export function grokEnv(parent: Record<string, string | undefined>, isolatedHome: string, grokHome: string): Record<string, string> {
  if (!isolatedHome || !grokHome || isolatedHome === grokHome) throw Error('Isolated HOME required')
  if (parent.HOME && isolatedHome === parent.HOME) throw Error('Isolated HOME must differ from the real HOME')
  const env: Record<string, string> = {}
  for (const [k, v] of Object.entries(parent)) if (v !== undefined && k !== 'DISCORD_BOT_TOKEN') env[k] = v
  env.HOME = isolatedHome; env.GROK_HOME = grokHome
  for (const k of DISABLED_DISCOVERY) env[k] = 'false'
  return env
}

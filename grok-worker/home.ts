/** Isolated HOME for grok (design §4.3): the real HOME's top-level entries as symlinks, minus the agent configs grok
 * would otherwise import (Claude plugins/MCP incl. the Discord plugin, hooks, skills). Rebuilt on every bridge start. */
import { existsSync, lstatSync, mkdirSync, readdirSync, rmSync, symlinkSync, unlinkSync } from 'node:fs'
import { join, resolve } from 'node:path'
export const EXCLUDED = ['.claude', '.claude.json', '.cursor', '.codex', '.grok']
export function buildIsolatedHome(realHome: string, dir: string): string {
  realHome = resolve(realHome); dir = resolve(dir)
  if (dir === realHome || realHome.startsWith(dir + '/')) throw Error('Isolated HOME must not contain the real HOME')
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  // Clear the previous mirror entry by entry: symlinks are unlinked (never followed); anything grok created here is ours.
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    if (lstatSync(p).isSymbolicLink()) unlinkSync(p); else rmSync(p, { recursive: true, force: true })
  }
  for (const e of readdirSync(realHome)) if (!EXCLUDED.includes(e)) symlinkSync(join(realHome, e), join(dir, e))
  for (const e of EXCLUDED) if (existsSync(join(dir, e)) || isLink(join(dir, e))) throw Error(`Isolation leak: ${e}`)
  return dir
}
const isLink = (p: string) => { try { return lstatSync(p).isSymbolicLink() } catch { return false } }

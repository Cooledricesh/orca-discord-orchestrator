import { realpathSync, existsSync } from 'node:fs'
import { join, sep, resolve } from 'node:path'
import { homedir } from 'node:os'
const inside = (path: string, root: string) => path === root || path.startsWith(root + sep)
export function assertPublicAttachment(file: string, stateDir: string, roots: string[] = []) {
  const real = realpathSync(file)
  const canonical = (path: string) => existsSync(path) ? realpathSync(path) : resolve(path)
  const state = canonical(stateDir), inbox = join(state, 'inbox')
  // Only the physical own inbox is public. A symlink to another root cannot qualify.
  const ownInbox = canonical(inbox) === inbox && inside(real, inbox)
  const secretRoots = [process.env.BOTS_DIR ?? join(homedir(), '.claude/channels/bots'), process.env.CODEX_HOME ?? join(homedir(), '.codex')].map(canonical)
  const privateRoots = [process.env.STATE_DIR_ROOT ?? join(process.env.ORCH_ROOT ?? resolve(import.meta.dir, '../..'), 'state'), ...roots].map(canonical)
  if ((inside(real, state) && !ownInbox) || secretRoots.some(root => inside(real, root)) || privateRoots.some(root => inside(real, root) && !(ownInbox && inside(state, root))) || /(?:^|\/)(?:\.env(?:\..*)?|auth\.json)$/.test(real)) throw Error('Refusing credential/state attachment')
}

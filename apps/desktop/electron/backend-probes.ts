/** Bounded backend probes. A file on disk is not proof of a usable runtime. */

import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'

import { buildDesktopBackendEnv } from './backend-env'
import { windowsShellCommand } from './windows-child-options'

/** Default probe budget. 5s false-negativeed healthy Windows cold starts (#61764). */
const DEFAULT_PROBE_TIMEOUT_MS = 15_000

/**
 * Resolve the backend probe timeout (ms).
 * Honours HERMES_PROBE_TIMEOUT_MS when it parses as a positive integer.
 */
function resolveProbeTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.HERMES_PROBE_TIMEOUT_MS

  if (raw == null || raw === '') {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  const n = Number.parseInt(String(raw), 10)

  if (!Number.isFinite(n) || n <= 0) {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  // Clamp absurd values (ms) so a typo can't hang startup forever.
  return Math.min(n, 120_000)
}

const PROBE_TIMEOUT_MS = resolveProbeTimeoutMs()

async function killProbeProcessTree(child: ChildProcess): Promise<boolean> {
  if (process.platform !== 'win32') {
    return child.kill('SIGKILL')
  }

  if (child.pid === undefined) {
    throw new Error('Cannot terminate runtime probe process tree without a process ID')
  }

  const taskkillPath = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe')

  return new Promise<boolean>((resolve, reject) => {
    const taskkill = spawn(taskkillPath, ['/PID', String(child.pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true
    })
    taskkill.once('error', reject)
    taskkill.once('close', (code, signal) => {
      if (code === 0) {
        resolve(true)
      } else {
        reject(new Error(`Runtime probe process-tree termination failed (${signal || code}): ${child.pid}`))
      }
    })
  })
}
class ProbeTimeoutError extends Error {}

function isTimeoutError(err: unknown): boolean {
  return err instanceof ProbeTimeoutError
}

/**
 * Run without blocking the event loop; on timeout only, retry once before failing.
 * Non-timeout failures (ENOENT, non-zero exit) fail immediately.
 */
async function execProbe(
  command: string,
  args: string[],
  options: {
    cwd?: string
    env?: NodeJS.ProcessEnv
    stdio: 'ignore'
    timeout: number
    shell?: boolean
    windowsHide?: boolean
  }
): Promise<void> {
  const { timeout, ...spawnOptions } = options
  const run = () =>
    new Promise<void>((resolve, reject) => {
      const child = spawn(command, args, spawnOptions)
      let deadlineTermination: Promise<boolean> | undefined
      const timer = setTimeout(() => {
        deadlineTermination = killProbeProcessTree(child)
        void deadlineTermination.catch(error => reject(error))
      }, timeout)

      child.once('exit', () => clearTimeout(timer))
      child.once('error', error => {
        clearTimeout(timer)
        reject(error)
      })
      child.once('close', (code, signal) => {
        clearTimeout(timer)
        void (async () => {
          let deadlineKilledChild = false

          try {
            deadlineKilledChild = (await deadlineTermination) ?? false
          } catch (error) {
            reject(error)
            return
          }

          if (deadlineKilledChild) {
            reject(new ProbeTimeoutError(`Runtime probe timed out after ${timeout} ms: ${command}`))
          } else if (code === 0 && !child.killed) {
            resolve()
          } else {
            reject(
              Object.assign(new Error(`Runtime probe failed: ${command} (${signal || code})`), {
                code,
                signal,
                killed: child.killed
              })
            )
          }
        })()
      })
    })

  try {
    await run()
  } catch (err) {
    if (!isTimeoutError(err)) {
      throw err
    }

    // One cold-cache / AV miss should not force hermes-setup --update (#61764).
    await run()
  }
}

/** Probe the checkout at cwd with the same dependency activation as launch. */
async function canImportHermesCli(
  pythonPath: string,
  opts: { env?: NodeJS.ProcessEnv; cwd?: string } = {}
): Promise<boolean> {
  if (!pythonPath) {
    return false
  }

  try {
    const env: NodeJS.ProcessEnv = { ...process.env, ...opts.env }

    // Bootstrap selects the committed generation before any dependency import.
    await execProbe(
      pythonPath,
      ['-c', 'import hermes_bootstrap; import hermes_yaml; import dotenv; import hermes_cli.config'],
      {
        cwd: opts.cwd,
        env: { ...env, ...buildDesktopBackendEnv({ currentEnv: env }) },
        stdio: 'ignore',
        timeout: PROBE_TIMEOUT_MS,
        windowsHide: true
      }
    )

    return true
  } catch {
    return false
  }
}

/**
 * Return true iff `<hermesCommand> --version` exits 0.
 *
 * Used to gate the "existing `hermes` on PATH" rung. Without this, a
 * stale hermes.cmd shim left behind by an uninstalled pip install (or
 * a half-built venv whose `hermes` entry-point points at a deleted
 * Python) survives findOnPath() and gets selected as the backend.
 *
 * We intentionally avoid invoking the command with the dashboard args
 * here -- `--version` is the cheapest "is this binary alive" smoke
 * test that every hermes_cli entry-point has supported since 0.1.
 *
 * @param {string} hermesCommand - Resolved absolute path to a hermes
 *   executable (or an interpreter+script wrapper).
 * @param {boolean} [opts.shell] - Whether to run through a shell. For
 *   .cmd/.bat shims on Windows spawn needs shell:true to find
 *   the cmd interpreter; mirrors the same flag isCommandScript() drives
 *   in resolveHermesBackend.
 * @returns {boolean}
 */
/**
 * An explicit desktop backend command is a deployment contract, not a PATH
 * discovery candidate. In particular, the Nix desktop wrapper points this at
 * its immutable, matching Hermes package; it must never fall through to the
 * mutable install-script bootstrap path if a best-effort probe is slow.
 */
function shouldTrustHermesOverride(hermesOverride?: string) {
  return typeof hermesOverride === 'string' && hermesOverride.trim().length > 0
}

async function verifyHermesCli(hermesCommand: string, opts?: { shell?: boolean }) {
  if (!hermesCommand) {
    return false
  }

  try {
    await execProbe(windowsShellCommand(hermesCommand, Boolean(opts?.shell)), ['--version'], {
      stdio: 'ignore',
      timeout: PROBE_TIMEOUT_MS,
      shell: Boolean(opts?.shell),
      windowsHide: true
    })

    return true
  } catch {
    return false
  }
}

export {
  canImportHermesCli,
  DEFAULT_PROBE_TIMEOUT_MS,
  execProbe,
  isTimeoutError,
  PROBE_TIMEOUT_MS,
  resolveProbeTimeoutMs,
  shouldTrustHermesOverride,
  verifyHermesCli
}

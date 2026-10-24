import { existsSync } from 'node:fs'
import path from 'node:path'

import { buildDesktopBackendEnv } from './backend-env'
import { execProbe, isTimeoutError, PROBE_TIMEOUT_MS } from './backend-probes'
import { resolveInstallationLauncher } from './updater-process'

export interface SourceBackend {
  kind: 'command' | 'python'
  label: string
  command: string
  args: string[]
  env: NodeJS.ProcessEnv
  root: string
  bootstrap: false
  shell: boolean
  local: 'installed'
  /** Only a final bounded probe timeout can use the existing-install recovery path. */
  verification: 'verified' | 'timed-out'
}

interface SourceOptions {
  isWindows?: boolean
  env?: NodeJS.ProcessEnv
  /** Test seam; production uses the bounded retry probe. */
  probe?: typeof execProbe
}

/** Keep the validated command. PM owns interpreter and generation selection. */
export async function resolveSourceInstallationBackend(
  root: string,
  args: string[],
  options: SourceOptions & { hermesHome?: string } = {}
): Promise<SourceBackend | null> {
  if (!existsSync(path.join(root, 'hermes_cli', 'main.py'))) {
    return null
  }

  const isWindows: boolean = options.isWindows ?? process.platform === 'win32'
  const launcher: string | null = resolveInstallationLauncher(root, isWindows, options.hermesHome)

  if (!launcher) {
    return null
  }

  const shell: boolean = isWindows && /\.(cmd|bat)$/i.test(launcher)
  const command: string = shell ? `"${launcher}"` : launcher
  const env: NodeJS.ProcessEnv = buildDesktopBackendEnv({ currentEnv: options.env ?? process.env })
  const probe = options.probe ?? execProbe
  let verification: SourceBackend['verification'] = 'verified'

  try {
    await probe(command, ['--version'], {
      cwd: root,
      env: { ...process.env, ...options.env, ...env },
      shell,
      stdio: 'ignore',
      timeout: PROBE_TIMEOUT_MS,
      windowsHide: true
    })
  } catch (error) {
    if (!isTimeoutError(error)) {
      return null
    }

    // execProbe retries once before returning this classified final timeout.
    // A source launcher that is merely slow is an existing installation;
    // startup owns its bounded recovery path.
    verification = 'timed-out'
  }

  return {
    kind: 'command',
    label: `Hermes at ${root}`,
    command,
    args: [...args],
    env,
    root,
    bootstrap: false,
    shell,
    local: 'installed',
    verification
  }
}

/** Runtime logging remains with source-backend recovery classification. */
export namespace resolveSourceInstallationBackend {
  export function logRuntimeUse(
    backend: { label: string; verification?: SourceBackend['verification'] },
    log: (message: string) => void
  ): void {
    if (backend.verification === 'timed-out') {
      log('[backend] source launcher verification timed out after its bounded retry; starting the existing install')
    }

    log(`[backend] using ${backend.label}`)
  }
}

/** Developer overrides retain their interpreter, even outside the checkout. */
export function createSourcePythonBackend(
  root: string,
  python: string | null,
  args: string[],
  options: SourceOptions = {}
): SourceBackend | null {
  if (!python) {
    return null
  }

  let command: string = python

  if ((options.isWindows ?? process.platform === 'win32') && /[\\/]pythonw\.exe$/i.test(python)) {
    // Use only the console interpreter beside the selected windowless one.
    const consolePython: string = python.replace(/pythonw\.exe$/i, 'python.exe')

    if (existsSync(consolePython)) {
      command = consolePython
    }
  }

  return {
    kind: 'python',
    label: `Hermes source at ${root}`,
    command,
    args: ['-m', 'hermes_cli.main', ...args],
    // The backend runs in the user's workspace cwd, and the selected
    // interpreter need not have this checkout installed: name it explicitly.
    // (The scrubbed inherited value could point at another checkout.)
    env: { ...buildDesktopBackendEnv({ currentEnv: options.env ?? process.env }), PYTHONPATH: root },
    root,
    bootstrap: false,
    shell: false,
    local: 'installed',
    verification: 'verified'
  }
}

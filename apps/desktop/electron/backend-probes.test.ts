/**
 * Tests for electron/backend-probes.ts.
 *
 * (Wired into pnpm run test:desktop:platforms in package.json.)
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  canImportHermesCli,
  DEFAULT_PROBE_TIMEOUT_MS,
  execProbe,
  isTimeoutError,
  PROBE_TIMEOUT_MS,
  resolveProbeTimeoutMs,
  shouldTrustHermesOverride,
  verifyHermesCli
} from './backend-probes'
import { createFirstRunSetupGate } from './first-run-setup-gate'
import { isSourceRuntimeUsable, resolveSourceInstallationBackend } from './source-backend'
import { chooseUpdaterArgs } from './windows-hermes-path'

// Resolve the host's own Node binary -- guaranteed to be on disk and
// runnable. We use it as both a stand-in for "a python that doesn't
// have hermes_cli" (since `node -c "import hermes_cli"` will exit
// non-zero) and as a way to script verifyHermesCli's success path
// (a tiny script we write to disk that exits 0 on --version).
const NODE_BIN = process.execPath

test('execProbe keeps the parent event loop available to the child', async () => {
  let unexpectedSocketError: Error | undefined

  const server = net.createServer(socket => {
    socket.on('error', error => {
      // A successful child exits immediately after reading the sentinel. On
      // Windows that peer close can surface as ECONNRESET on the server side.
      if ((error as NodeJS.ErrnoException).code !== 'ECONNRESET') {
        unexpectedSocketError ??= error
      }
    })
    socket.end('pong')
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })

  const address = server.address()
  assert.ok(address && typeof address === 'object')

  const childScript = `
    const net = require('node:net')
    let reply = ''
    const socket = net.createConnection(${address.port}, '127.0.0.1')
    socket.setEncoding('utf8')
    socket.on('data', (chunk) => { reply += chunk })
    socket.on('end', () => process.exit(reply === 'pong' ? 0 : 1))
    socket.on('error', () => process.exit(1))
  `

  try {
    await execProbe(NODE_BIN, ['-e', childScript], {
      stdio: 'ignore',
      timeout: 5_000,
      windowsHide: true
    })
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close(error => (error ? reject(error) : resolve()))
    })
  }

  assert.ifError(unexpectedSocketError)
})

// Self-expire with a failure so the test also finishes on a build that never
// enforces the probe deadline.
const slowProbe = `
  const fs = require('node:fs')
  const [attempts, scenario] = process.argv.slice(1)
  fs.appendFileSync(attempts, 'x')
  const first = fs.readFileSync(attempts, 'utf8') === 'x'
  if (first || scenario === 'both-slow') {
    setTimeout(() => process.exit(91), 2000)
  } else {
    process.exit(scenario === 'retry-fails' ? 23 : 0)
  }
`
const probeOptions = {
  stdio: 'ignore' as const,
  timeout: 650,
  windowsHide: true
}

test('a real probe deadline allows exactly one retry, which can succeed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-probe-retry-'))
  const attempts = path.join(root, 'attempts')

  try {
    await execProbe(NODE_BIN, ['-e', slowProbe, attempts, 'retry-succeeds'], probeOptions)
    assert.equal(fs.readFileSync(attempts, 'utf8'), 'xx')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

function withSourceFixture(callback: (root: string, launcher: string) => Promise<void>) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-source-probe-'))
  const launcher = path.join(root, '.hermes', 'bin', process.platform === 'win32' ? 'hermes.exe' : 'hermes')

  return (async () => {
    try {
      fs.mkdirSync(path.join(root, 'hermes_cli'))
      fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '')
      fs.mkdirSync(path.dirname(launcher), { recursive: true })
      fs.writeFileSync(launcher, 'fixture; the probe seam selects the test executable')
      await callback(root, launcher)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })()
}

test('two real launcher probe deadlines keep the existing backend out of first-run setup', async () => {
  await withSourceFixture(async (root, launcher) => {
    const attempts = path.join(root, 'attempts')
    const args = ['gateway', 'ensure', '--json']
    const backend = await resolveSourceInstallationBackend(root, args, {
      probe: (command, argv, options) => {
        assert.equal(command, launcher)
        assert.deepEqual(argv, ['--version'])

        return execProbe(NODE_BIN, ['-e', slowProbe, attempts, 'both-slow'], {
          ...options,
          ...probeOptions
        })
      }
    })

    assert.ok(backend, 'the final bounded timeout must retain the source install')
    assert.equal(fs.readFileSync(attempts, 'utf8'), 'xx')
    assert.equal(backend.command, launcher)
    assert.deepEqual(backend.args, args)
    assert.equal(backend.bootstrap, false)
    assert.equal(backend.local, 'installed')
    assert.equal(backend.verification, 'timed-out')
    assert.equal(createFirstRunSetupGate().shouldGate(backend), false)
  })
})

async function sourceUpdaterArgs(root: string, probe: typeof execProbe): Promise<string[]> {
  // Use only the fixture launcher, never an ambient Hermes home.
  const runtimeUsable = await isSourceRuntimeUsable(root, {
    hermesHome: path.join(root, '.hermes'),
    probe
  })
  return chooseUpdaterArgs({ runtimeUsable }, 'release/1.2')
}

test('source updater selects update after successful launcher verification', async () => {
  await withSourceFixture(async (root, launcher) => {
    const args = await sourceUpdaterArgs(root, (command, argv, options) => {
      assert.equal(command, launcher)
      assert.deepEqual(argv, ['--version'])
      return execProbe(NODE_BIN, ['--version'], options)
    })

    assert.deepEqual(args, ['--update', '--branch', 'release/1.2'])
  })
})

test('source updater selects repair after two real launcher probe deadlines', async () => {
  await withSourceFixture(async (root, launcher) => {
    const attempts = path.join(root, 'attempts')
    const args = await sourceUpdaterArgs(root, (command, argv, options) => {
      assert.equal(command, launcher)
      assert.deepEqual(argv, ['--version'])
      return execProbe(NODE_BIN, ['-e', slowProbe, attempts, 'both-slow'], {
        ...options,
        ...probeOptions
      })
    })

    assert.equal(fs.readFileSync(attempts, 'utf8'), 'xx')
    assert.deepEqual(args, ['--repair', '--branch', 'release/1.2'])
  })
})

test('source updater selects repair when the launcher is missing', async () => {
  await withSourceFixture(async (root, launcher) => {
    fs.unlinkSync(launcher)
    let attempts = 0
    const args = await sourceUpdaterArgs(root, async () => {
      attempts += 1
    })

    assert.equal(attempts, 0)
    assert.deepEqual(args, ['--repair', '--branch', 'release/1.2'])
  })
})

test('source updater selects repair when the launcher exits nonzero', async () => {
  await withSourceFixture(async (root, launcher) => {
    const attempts = path.join(root, 'attempts')
    const args = await sourceUpdaterArgs(root, (command, argv, options) => {
      assert.equal(command, launcher)
      assert.deepEqual(argv, ['--version'])
      const script = `require('node:fs').appendFileSync(process.argv[1], 'x'); process.exit(23)`
      return execProbe(NODE_BIN, ['-e', script, attempts], options)
    })

    assert.equal(fs.readFileSync(attempts, 'utf8'), 'x')
    assert.deepEqual(args, ['--repair', '--branch', 'release/1.2'])
  })
})

test('non-timeout source launcher failures still fall through', async () => {
  await withSourceFixture(async (root, launcher) => {
    const attempts = path.join(root, 'attempts')
    const resolveWith = (probe: typeof execProbe) => resolveSourceInstallationBackend(root, ['serve'], { probe })
    const run = (script: string) =>
      execProbe(NODE_BIN, ['-e', script, attempts], {
        ...probeOptions,
        timeout: 4000
      })
    const record = `require('node:fs').appendFileSync(process.argv[1], 'x'); `

    // Also check successful verification does not get tagged as recovery.
    const verified = await resolveWith(() => run('process.exit(0)'))
    assert.ok(verified)
    assert.equal(verified.verification, undefined)

    for (const script of ['process.exit(23)', `process.kill(process.pid, 'SIGTERM')`]) {
      fs.writeFileSync(attempts, '')
      assert.equal(await resolveWith(() => run(record + script)), null)
      assert.equal(fs.readFileSync(attempts, 'utf8'), 'x', `must not retry ${script}`)
    }

    fs.writeFileSync(attempts, '')
    assert.equal(
      await resolveWith(() => execProbe(NODE_BIN, ['-e', slowProbe, attempts, 'retry-fails'], probeOptions)),
      null
    )
    assert.equal(fs.readFileSync(attempts, 'utf8'), 'xx')

    const missing = path.join(root, 'missing.exe')
    await assert.rejects(execProbe(missing, [], probeOptions), {
      code: 'ENOENT'
    })
    assert.equal(await resolveWith(() => execProbe(missing, [], probeOptions)), null)
    assert.equal(
      await resolveWith(async () => {
        throw Object.assign(new Error('permission denied'), { code: 'EACCES' })
      }),
      null
    )

    fs.unlinkSync(launcher)
    let probed = false
    assert.equal(
      await resolveWith(async () => {
        probed = true
      }),
      null
    )
    assert.equal(probed, false)
  })
})

test.skipIf(process.platform === 'win32')('a real non-executable source launcher is not recovered', async () => {
  await withSourceFixture(async (root, launcher) => {
    fs.chmodSync(launcher, 0o644)
    await assert.rejects(execProbe(launcher, [], probeOptions), {
      code: 'EACCES'
    })
    assert.equal(await resolveSourceInstallationBackend(root, ['serve']), null)
  })
})

test('timeout-shaped errors without probe deadline provenance are not timeouts', () => {
  for (const error of [
    { code: 'ETIMEDOUT' },
    { signal: 'SIGTERM', killed: false },
    { signal: 'SIGTERM', killed: true },
    { signal: 'SIGKILL', killed: true }
  ]) {
    assert.equal(isTimeoutError(error), false, JSON.stringify(error))
  }
})

test('canImportHermesCli returns false when path is falsy', async () => {
  assert.equal(await canImportHermesCli(''), false)
  assert.equal(await canImportHermesCli(null), false)
  assert.equal(await canImportHermesCli(undefined), false)
})

test('canImportHermesCli returns false when interpreter cannot run -c', async () => {
  // node IS an interpreter, but `node -c "import hermes_cli"` is a
  // SyntaxError -- different exit reason from a real Python's
  // ModuleNotFoundError, but the predicate is "exit 0 or not" and
  // both land on "not", which is exactly what we want for the
  // resolver fall-through.
  assert.equal(await canImportHermesCli(NODE_BIN), false)
})

test('canImportHermesCli returns false when binary does not exist', async () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(await canImportHermesCli(ghost), false)
})

test('explicit Hermes override is authoritative', () => {
  assert.equal(shouldTrustHermesOverride('/nix/store/abc/bin/hermes'), true)
})

test('empty Hermes override is not authoritative', () => {
  assert.equal(shouldTrustHermesOverride(''), false)
  assert.equal(shouldTrustHermesOverride(undefined), false)
})

test('verifyHermesCli returns false when command is falsy', async () => {
  assert.equal(await verifyHermesCli(''), false)
  assert.equal(await verifyHermesCli(null), false)
  assert.equal(await verifyHermesCli(undefined), false)
})

test('verifyHermesCli returns false when binary does not exist', async () => {
  const ghost = path.join(os.tmpdir(), 'hermes-probes-ghost-' + Date.now() + '.exe')
  assert.equal(await verifyHermesCli(ghost), false)
})

test('verifyHermesCli accepts an actual zero-exit executable', async (): Promise<void> => {
  assert.equal(await verifyHermesCli(NODE_BIN), true)
})

// #74064: with shell:true the command line goes through a shell (cmd.exe on
// Windows, /bin/sh here), which truncates an unquoted executable at the first
// space — `C:\Users\John Doe\...\hermes.cmd --version` runs `C:\Users\John`.
// The same truncation reproduces on POSIX sh, so this is a real behavioral
// test of the quoting, not a platform-conditional one. Windows gets its own
// lane: there the quoted form goes through cmd.exe /s semantics instead.
test.skipIf(process.platform === 'win32')(
  'verifyHermesCli quotes a spaced executable path when probing through a shell',
  async (): Promise<void> => {
    const spacedDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes probe-'))
    const spacedCmd = path.join(spacedDir, 'hermes.cmd')
    fs.writeFileSync(spacedCmd, '#!/bin/sh\nexit 0\n', { mode: 0o755 })
    fs.chmodSync(spacedCmd, 0o755)

    try {
      // Unquoted (the pre-fix wiring): the shell truncates at the space → 127.
      await execProbe(spacedCmd, ['--version'], {
        stdio: 'ignore',
        timeout: 5_000,
        shell: true,
        windowsHide: true
      })
      assert.fail('unquoted spaced path must fail through the shell')
    } catch {
      // expected: the shell could not run the truncated command
    }

    // verifyHermesCli wraps the same failure: off-Windows the helper is a
    // no-op (POSIX sh truncates identically), so the spaced-path probe
    // reports the backend missing — exactly the #74064 symptom. The quoting
    // itself is covered by the windowsShellCommand unit tests and runs on
    // the Windows lane.
    assert.equal(await verifyHermesCli(spacedCmd, { shell: true }), false)

    // Direct execution (shell: false) never goes through a shell, so a
    // spaced path works as-is — the fix must not leak into the non-shell path.
    assert.equal(await verifyHermesCli(spacedCmd, { shell: false }), true)

    fs.rmSync(spacedDir, { recursive: true, force: true })
  }
)

test('default probe timeout is 15s (not the old 5s death-loop value)', () => {
  assert.equal(DEFAULT_PROBE_TIMEOUT_MS, 15_000)
  // Module constant uses process.env at load time; with no override it
  // matches the default (tests run without HERMES_PROBE_TIMEOUT_MS).
  assert.equal(PROBE_TIMEOUT_MS, DEFAULT_PROBE_TIMEOUT_MS)
})

test('resolveProbeTimeoutMs honours HERMES_PROBE_TIMEOUT_MS', () => {
  assert.equal(resolveProbeTimeoutMs({}), DEFAULT_PROBE_TIMEOUT_MS)
  assert.equal(resolveProbeTimeoutMs({ HERMES_PROBE_TIMEOUT_MS: '30000' }), 30_000)
  assert.equal(resolveProbeTimeoutMs({ HERMES_PROBE_TIMEOUT_MS: '0' }), DEFAULT_PROBE_TIMEOUT_MS)
  assert.equal(resolveProbeTimeoutMs({ HERMES_PROBE_TIMEOUT_MS: 'nope' }), DEFAULT_PROBE_TIMEOUT_MS)
  // Cap runaway values
  assert.equal(resolveProbeTimeoutMs({ HERMES_PROBE_TIMEOUT_MS: '999999' }), 120_000)
})

interface WindowsProcessInfo {
  ProcessId: number
  ParentProcessId: number
  Name: string
  CommandLine: string
}

function windowsProcessSnapshot(pids: number[]): WindowsProcessInfo[] {
  const filter = pids.map(pid => `ProcessId = ${pid}`).join(' OR ')
  const command = `$items = @(Get-CimInstance Win32_Process -Filter '${filter}'); $items | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress`
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], {
    encoding: 'utf8',
    windowsHide: true
  }).trim()
  if (!output) return []
  const parsed = JSON.parse(output) as WindowsProcessInfo | WindowsProcessInfo[]
  return Array.isArray(parsed) ? parsed : [parsed]
}

interface FixtureProcess {
  pid: number
  parentPid: number
  token: string
}

function readFixtureProcesses(file: string): FixtureProcess[] {
  if (!fs.existsSync(file)) return []
  return fs
    .readFileSync(file, 'utf8')
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .map(line => JSON.parse(line) as FixtureProcess)
}

async function waitForFixtureProcesses(file: string, count: number, timeout: number): Promise<FixtureProcess[]> {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const processes = readFixtureProcesses(file)
    if (processes.length >= count) return processes
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  throw new Error(`test-owned .cmd wrapper did not launch ${count} children before the deadline`)
}

async function cleanFixtureProcesses(
  records: FixtureProcess[],
  script: string,
  wrapper: string,
  token: string
): Promise<void> {
  const targets = records.flatMap(record => [
    {
      pid: record.parentPid,
      isOwned: (processInfo: WindowsProcessInfo) =>
        processInfo.Name.toLowerCase() === 'cmd.exe' &&
        processInfo.CommandLine.toLowerCase().includes(wrapper.toLowerCase())
    },
    {
      pid: record.pid,
      isOwned: (processInfo: WindowsProcessInfo) =>
        processInfo.Name.toLowerCase() === 'node.exe' &&
        processInfo.ParentProcessId === record.parentPid &&
        processInfo.CommandLine.toLowerCase().includes(script.toLowerCase()) &&
        processInfo.CommandLine.includes(token)
    }
  ])
  const pids = targets.map(target => target.pid)
  const live = windowsProcessSnapshot(pids)

  for (const target of targets) {
    const processInfo = live.find(processInfo => processInfo.ProcessId === target.pid)
    if (!processInfo) continue
    if (!target.isOwned(processInfo)) {
      throw new Error(`Fixture PID ${target.pid} no longer matches its recorded process; refusing to terminate it`)
    }

    try {
      process.kill(target.pid, 'SIGKILL')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error
    }
  }

  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    const remaining = windowsProcessSnapshot(pids)

    for (const processInfo of remaining) {
      const target = targets.find(target => target.pid === processInfo.ProcessId)
      if (target && !target.isOwned(processInfo)) {
        throw new Error(`Fixture PID ${processInfo.ProcessId} changed identity; refusing to terminate it`)
      }
    }
    if (remaining.length === 0) return
    await new Promise(resolve => setTimeout(resolve, 25))
  }

  throw new Error('test-owned fixture processes remained alive after cleanup')
}
test.skipIf(process.platform !== 'win32')(
  'a real .cmd source probe kills its child tree before the bounded retry',
  async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes .cmd probe-'))
    const wrapper = path.join(root, '.hermes', 'bin', 'hermes.cmd')
    const script = path.join(root, 'test-owned child.cjs')
    const recordsFile = path.join(root, 'processes.jsonl')
    const token = `fixture-${process.pid}-${Date.now()}`
    fs.mkdirSync(path.dirname(wrapper), { recursive: true })
    fs.mkdirSync(path.join(root, 'hermes_cli'), { recursive: true })
    fs.writeFileSync(path.join(root, 'hermes_cli', 'main.py'), '')
    fs.writeFileSync(
      script,
      `
    const fs = require('node:fs')
    const [recordsFile, token] = process.argv.slice(2)
    if (process.env.HERMES_PROBE_FIXTURE_SUCCESS === '1') process.exit(0)
    fs.appendFileSync(recordsFile, JSON.stringify({ pid: process.pid, parentPid: process.ppid, token }) + '\\n')
    setInterval(() => {}, 1000)
  `
    )
    fs.writeFileSync(wrapper, `@echo off\r\n"${process.execPath}" "${script}" "${recordsFile}" "${token}" %*\r\n`)

    const probe: typeof execProbe = (command, args, options) => {
      assert.equal(command, `"${wrapper}"`)
      assert.deepEqual(args, ['--version'])
      assert.equal(options.shell, true)
      return execProbe(command, args, { ...options, timeout: 3500 })
    }

    try {
      const verified = await resolveSourceInstallationBackend(root, ['serve'], {
        env: { ...process.env, HERMES_PROBE_FIXTURE_SUCCESS: '1' },
        probe
      })
      assert.ok(verified)
      assert.equal(verified.verification, undefined)

      const outcome = resolveSourceInstallationBackend(root, ['serve'], {
        env: { ...process.env, HERMES_PROBE_FIXTURE_SUCCESS: '0' },
        probe
      }).then(
        backend => ({ ok: true as const, backend }),
        error => ({ ok: false as const, error })
      )
      let records = await waitForFixtureProcesses(recordsFile, 1, 2500)
      const record = records[0]
      {
        const initial = windowsProcessSnapshot([record.pid, record.parentPid])
        const child = initial.find(process => process.ProcessId === record.pid)
        const parent = initial.find(process => process.ProcessId === record.parentPid)
        assert.equal(child?.ParentProcessId, record.parentPid)
        assert.equal(child?.Name.toLowerCase(), 'node.exe')
        assert.ok(child?.CommandLine.toLowerCase().includes(script.toLowerCase()))
        assert.equal(parent?.Name.toLowerCase(), 'cmd.exe')
        assert.ok(parent?.CommandLine.toLowerCase().includes(wrapper.toLowerCase()))
      }

      records = await waitForFixtureProcesses(recordsFile, 2, 7000)
      const first = windowsProcessSnapshot([records[0].pid])[0]
      assert.ok(
        !first ||
          first.Name.toLowerCase() !== 'node.exe' ||
          !first.CommandLine.toLowerCase().includes(script.toLowerCase()),
        'retry must start only after the first child tree has exited'
      )
      const second = records[1]
      const secondSnapshot = windowsProcessSnapshot([second.pid, second.parentPid])
      const secondChild = secondSnapshot.find(process => process.ProcessId === second.pid)
      const secondParent = secondSnapshot.find(process => process.ProcessId === second.parentPid)
      assert.equal(secondChild?.ParentProcessId, second.parentPid)
      assert.equal(secondChild?.Name.toLowerCase(), 'node.exe')
      assert.ok(secondChild?.CommandLine.toLowerCase().includes(script.toLowerCase()))
      assert.equal(secondParent?.Name.toLowerCase(), 'cmd.exe')
      assert.ok(secondParent?.CommandLine.toLowerCase().includes(wrapper.toLowerCase()))
      const result = await outcome
      assert.equal(result.ok, true)
      if (result.ok) {
        assert.ok(result.backend)
        assert.equal(result.backend.verification, 'timed-out')
        assert.equal(result.backend.command, `"${wrapper}"`)
        assert.deepEqual(result.backend.args, ['serve'])
        assert.equal(result.backend.bootstrap, false)
        assert.equal(result.backend.shell, true)
      }
      records = readFixtureProcesses(recordsFile)
      assert.equal(records.length, 2, 'the deadline must trigger exactly one bounded retry')
      assert.ok(records.every(record => record.token === token))

      const live = records.flatMap(record => {
        const process = windowsProcessSnapshot([record.pid])[0]
        return process &&
          process.Name.toLowerCase() === 'node.exe' &&
          process.CommandLine.toLowerCase().includes(script.toLowerCase()) &&
          process.CommandLine.includes(token)
          ? [record.pid]
          : []
      })
      assert.deepEqual(live, [], 'timing out the .cmd wrapper must not leave fixture children alive')
    } finally {
      const records = readFixtureProcesses(recordsFile)
      await cleanFixtureProcesses(records, script, wrapper, token)
      fs.rmSync(root, { recursive: true, force: true })
    }
  },
  60000
)

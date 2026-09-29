import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  PoolBackendUpdateBlockedError,
  startPoolBackendAfterUpdateClearance
} from './pool-update-gate'

test.each(['timeout', 'cancelled'] as const)(
  'pool startup does not call spawn when update clearance is %s',
  async outcome => {
    let spawns = 0
    const visibleFailures: string[] = []

    await assert.rejects(
      () => startPoolBackendAfterUpdateClearance(outcome, () => true, () => {
        spawns += 1
        return 'spawned'
      }, reason => visibleFailures.push(reason)),
      PoolBackendUpdateBlockedError
    )

    assert.equal(spawns, 0)
    assert.deepEqual(visibleFailures, [outcome])
  }
)

test.each(['clear', 'finished'] as const)(
  'pool startup invokes spawn only after %s update clearance',
  async outcome => {
    let spawns = 0

    const result = await startPoolBackendAfterUpdateClearance(outcome, () => true, () => {
      spawns += 1
      return 'spawned'
    })

    assert.equal(result, 'spawned')
    assert.equal(spawns, 1)
  }
)

test('pool startup refuses spawn when the update gate re-closes after wait clearance', async () => {
  let spawns = 0
  const visibleFailures: string[] = []

  await assert.rejects(
    () => startPoolBackendAfterUpdateClearance(
      'finished',
      () => false,
      () => {
        spawns += 1

        return 'spawned'
      },
      reason => visibleFailures.push(reason)
    ),
    PoolBackendUpdateBlockedError
  )

  assert.equal(spawns, 0)
  assert.deepEqual(visibleFailures, ['reclosed'])
})
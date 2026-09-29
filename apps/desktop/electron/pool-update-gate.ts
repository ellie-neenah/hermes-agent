import {
  backendStartAllowedAfterUpdateWait,
  type UpdateClearanceOutcome
} from './update-gate'

export type PoolBackendUpdateBlockReason = Exclude<UpdateClearanceOutcome, 'clear' | 'finished'> | 'reclosed'

export class PoolBackendUpdateBlockedError extends Error {
  constructor(reason: PoolBackendUpdateBlockReason) {
    const message = reason === 'timeout'
      ? 'The update is still in progress after the bounded wait.'
      : reason === 'cancelled'
        ? 'The pool backend start was cancelled while an update was in progress.'
        : 'The update gate closed again before the pool backend could start.'

    super(`${message} Retry after the update finishes.`)
    this.name = 'PoolBackendUpdateBlockedError'
  }
}

/**
 * The final pooled-backend start boundary. Keep the spawn closure inside this
 * gate so timeout/cancellation cannot accidentally fall through to spawn, and
 * recheck the live gate because it can close after the waiter reports clear.
 */
export async function startPoolBackendAfterUpdateClearance<T>(
  outcome: UpdateClearanceOutcome,
  isUpdateGateOpen: () => boolean,
  spawn: () => T | Promise<T>,
  onBlocked?: (reason: PoolBackendUpdateBlockReason) => void
): Promise<T> {
  const block = (reason: PoolBackendUpdateBlockReason): never => {
    onBlocked?.(reason)
    throw new PoolBackendUpdateBlockedError(reason)
  }

  if (outcome === 'timeout' || outcome === 'cancelled') {
    return block(outcome)
  }

  if (!backendStartAllowedAfterUpdateWait(outcome)) {
    throw new Error(`Unexpected update-clearance outcome: ${outcome}`)
  }

  if (!isUpdateGateOpen()) {
    return block('reclosed')
  }

  return spawn()
}

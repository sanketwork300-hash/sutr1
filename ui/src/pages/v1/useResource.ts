import { useCallback, useEffect, useState } from 'react'
import { describeError } from '@/components/sutr'

export interface Resource<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
}

/**
 * Load one thing, and say honestly which of the three states it is in.
 *
 * The three are deliberately separate rather than collapsed into
 * `data ?? null`: "still loading", "failed, and here is why", and "loaded, and
 * there is nothing" are different answers, and a page that renders an empty
 * table for all three tells the reader something false about two of them.
 */
export function useResource<T>(load: () => Promise<T>, deps: unknown[] = []): Resource<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  // The loader closes over its own dependencies; the caller declares them.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps)

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    run()
      .then((value) => {
        if (live) setData(value)
      })
      .catch((caught) => {
        if (live) setError(describeError(caught).message)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [run, nonce])

  return { data, loading, error, reload: () => setNonce((n) => n + 1) }
}

/**
 * Run an action, keep whatever it said, and never leave the page believing a
 * refusal was a success.
 */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const run = useCallback(async (action: () => Promise<unknown>, success?: string) => {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      await action()
      if (success) setMessage(success)
      return true
    } catch (caught) {
      setError(describeError(caught).message)
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  return { busy, error, message, run, clear: () => (setError(null), setMessage(null)) }
}

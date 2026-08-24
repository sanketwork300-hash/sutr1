import { useCallback, useEffect, useState } from 'react'
import { api, type AuthorizeResult, type ConnectionProviderInfo } from '@/api/client'

/** Where the provider sends the browser back. A dedicated route rather than
 *  the page the user was on, so the popup can close itself without that page
 *  ever rendering inside it. */
export const CONNECTION_CALLBACK_PATH = '/connections/callback'

export interface ConnectionsState {
  providers: ConnectionProviderInfo[]
  loading: boolean
  error: string
  reload: () => Promise<void>
  find: (providerId: string) => ConnectionProviderInfo | undefined
}

/**
 * The list of providers, who is connected to each, and a way to refresh it.
 *
 * The refresh matters more than it looks: a connection is completed in a
 * different window, so the only way this page learns about it is by asking
 * again once that window reports back.
 */
export function useProviderConnections(enabled = true): ConnectionsState {
  const [providers, setProviders] = useState<ConnectionProviderInfo[]>([])
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    try {
      const result = await api.connections.list()
      setProviders(result.providers)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load connected accounts')
    } finally {
      setLoading(false)
    }
  }, [enabled])

  useEffect(() => {
    void reload()
  }, [reload])

  const find = useCallback(
    (providerId: string) => providers.find((entry) => entry.id === providerId),
    [providers],
  )

  return { providers, loading, error, reload, find }
}

/**
 * Open a provider's consent screen in a popup and resolve when it closes.
 *
 * A popup rather than a full-page redirect because the MCP builder is a
 * multi-stage form: navigating away to GitHub and back would discard whatever
 * the user had already filled in. If the browser blocks the popup we fall
 * back to a same-tab redirect, which still works — it just costs the form.
 */
export async function startRedirectConnect(
  providerId: string,
  redirectAfter = CONNECTION_CALLBACK_PATH,
): Promise<AuthorizeResult> {
  // Opened before the await: a popup opened after one is treated as
  // unrequested by every browser and blocked.
  const popup = window.open('', '_blank', 'width=680,height=760')
  let result: AuthorizeResult
  try {
    result = await api.connections.authorize(providerId, redirectAfter)
  } catch (e) {
    popup?.close()
    throw e
  }
  if (!result.authorization_url) {
    popup?.close()
    throw new Error('The server did not return an authorization URL.')
  }
  if (popup && !popup.closed) {
    popup.location.href = result.authorization_url
  } else {
    window.location.href = result.authorization_url
  }
  return result
}

/** Message the callback route posts back to whoever opened it. */
export interface ConnectionMessage {
  source: 'sutr-connection'
  provider: string
  status: string
  detail?: string
}

export function isConnectionMessage(data: unknown): data is ConnectionMessage {
  return (
    typeof data === 'object' &&
    data !== null &&
    (data as { source?: unknown }).source === 'sutr-connection'
  )
}

/**
 * Run `onFinished` when a connection popup reports back.
 *
 * Same-origin only: the message carries no secret, but a listener that
 * accepted any origin would let any page in another tab convince this one
 * that a connection succeeded.
 */
export function useConnectionMessages(onFinished: (message: ConnectionMessage) => void) {
  useEffect(() => {
    function handle(event: MessageEvent) {
      if (event.origin !== window.location.origin) return
      if (!isConnectionMessage(event.data)) return
      onFinished(event.data)
    }
    window.addEventListener('message', handle)
    return () => window.removeEventListener('message', handle)
  }, [onFinished])
}

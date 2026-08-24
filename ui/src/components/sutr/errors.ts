import { ApiError } from '@/api/client'

/**
 * Pulls the operator-facing facts out of whatever the API layer threw: the
 * message the server actually sent, plus the request ID when there is one so a
 * report can be traced in the logs. Stack traces never reach the UI.
 */
export function describeError(error: unknown): { message: string; requestId?: string } {
  if (error instanceof ApiError) {
    const body = error.body as { request_id?: string } | null
    return { message: error.message, requestId: body?.request_id }
  }
  if (error instanceof Error) return { message: error.message }
  return { message: 'The request could not be completed.' }
}

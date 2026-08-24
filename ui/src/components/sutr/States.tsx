import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

export function SutrSpinner({ size = 14 }: { size?: number }) {
  return (
    <span
      className="sutr-btn__spinner"
      style={{ width: size, height: size, color: 'var(--text-faint)' }}
      role="status"
      aria-label="Loading"
    />
  )
}

export function SutrSkeleton({
  height = 14,
  width = '100%',
  radius,
}: {
  height?: number
  width?: number | string
  radius?: number
}) {
  return (
    <span
      className="sutr-skeleton"
      aria-hidden="true"
      style={{ display: 'block', height, width, borderRadius: radius }}
    />
  )
}

export function SutrEmpty({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode
  title: string
  body?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="sutr-empty">
      {icon ? <span className="sutr-empty__icon">{icon}</span> : null}
      <span className="sutr-empty__title">{title}</span>
      {body ? <span className="sutr-empty__body">{body}</span> : null}
      {action}
    </div>
  )
}

/**
 * Errors say what happened, why, and what to do next — plus the request ID
 * when the server sent one, so a report can be traced in the logs. Stack
 * traces are never surfaced here.
 */
export function SutrError({
  what,
  why,
  action,
  meta,
}: {
  what: string
  why?: ReactNode
  action?: ReactNode
  meta?: Record<string, string | null | undefined>
}) {
  const entries = Object.entries(meta ?? {}).filter(([, v]) => Boolean(v))
  return (
    <div className="sutr-error" role="alert">
      <AlertTriangle size={16} style={{ color: 'var(--red)', flexShrink: 0, marginTop: 1 }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0, flex: 1 }}>
        <span className="sutr-error__title">{what}</span>
        {why ? <span className="sutr-error__detail">{why}</span> : null}
        {entries.length > 0 ? (
          <span className="sutr-error__meta">
            {entries.map(([k, v]) => (
              <span key={k}>
                {k}: {v}
              </span>
            ))}
          </span>
        ) : null}
        {action ? <span style={{ display: 'flex', gap: 6, marginTop: 2 }}>{action}</span> : null}
      </div>
    </div>
  )
}

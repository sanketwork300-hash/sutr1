import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

interface MetricProps {
  label: string
  /** `null` means "not loaded yet"; render a skeleton rather than a zero. */
  value: number | string | null
  icon?: ReactNode
  foot?: ReactNode
  /** Makes the whole tile a route link — metrics should lead somewhere. */
  to?: string
  /** Dims the value when the real number is legitimately zero. */
  muted?: boolean
}

export function SutrMetric({ label, value, icon, foot, to, muted }: MetricProps) {
  const body = (
    <>
      <span className="sutr-metric__label">
        {icon}
        {label}
      </span>
      {value === null ? (
        <span className="sutr-skeleton" style={{ height: 26, width: 56 }} aria-hidden="true" />
      ) : (
        <span
          className={`sutr-metric__value${muted ? ' sutr-metric__value--muted' : ''}`}
          style={typeof value === 'string' ? { fontSize: 18, letterSpacing: '-0.02em' } : undefined}
        >
          {typeof value === 'number' ? value.toLocaleString() : value}
        </span>
      )}
      <span className="sutr-metric__foot">{foot}</span>
    </>
  )

  if (to) {
    return (
      <Link to={to} className="sutr-metric">
        {body}
      </Link>
    )
  }
  return <div className="sutr-metric">{body}</div>
}

import type { ReactNode } from 'react'
import { describeStatus, type StatusDomain, type Tone } from './status'

interface BadgeProps {
  tone?: Tone
  /** A dot reads as an operational state; without it the badge is a label. */
  dot?: boolean
  live?: boolean
  /** Sentence case instead of the uppercase operational style. */
  plain?: boolean
  title?: string
  children: ReactNode
}

export function SutrBadge({ tone = 'neutral', dot, live, plain, title, children }: BadgeProps) {
  return (
    <span
      className={['sutr-badge', `sutr-badge--${tone}`, plain ? 'sutr-badge--plain' : '']
        .filter(Boolean)
        .join(' ')}
      title={title}
    >
      {dot ? (
        <span
          className={['sutr-badge__dot', live ? 'sutr-badge__dot--live' : ''].join(' ')}
          aria-hidden="true"
        />
      ) : null}
      {children}
    </span>
  )
}

/** Renders a backend status through the shared operational vocabulary. */
export function SutrStatus({
  domain,
  value,
  title,
}: {
  domain: StatusDomain
  value: string | null | undefined
  title?: string
}) {
  const { label, tone, live } = describeStatus(domain, value)
  return (
    <SutrBadge tone={tone} dot live={live} title={title}>
      {label}
    </SutrBadge>
  )
}

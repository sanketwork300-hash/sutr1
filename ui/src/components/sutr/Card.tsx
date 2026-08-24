import type { HTMLAttributes, ReactNode } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** `flush` sits directly on the page ground; `elevated` floats above it. */
  tone?: 'default' | 'flush' | 'elevated'
  children?: ReactNode
}

export function SutrCard({ tone = 'default', className, children, ...rest }: CardProps) {
  const cls = ['sutr-card', tone !== 'default' ? `sutr-card--${tone}` : '', className ?? '']
    .filter(Boolean)
    .join(' ')
  return (
    <div className={cls} {...rest}>
      {children}
    </div>
  )
}

export function SutrCardHeader({
  title,
  meta,
  actions,
  icon,
}: {
  title: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="sutr-card__header">
      {icon}
      <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 1 }}>
        <h3 className="sutr-card__title">{title}</h3>
        {meta ? <span className="sutr-meta">{meta}</span> : null}
      </div>
      {actions ? (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
          {actions}
        </div>
      ) : null}
    </div>
  )
}

export function SutrCardBody({ children, className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={['sutr-card__body', className ?? ''].filter(Boolean).join(' ')} {...rest}>
      {children}
    </div>
  )
}

export function SutrCardFooter({ children, className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={['sutr-card__footer', className ?? ''].filter(Boolean).join(' ')} {...rest}>
      {children}
    </div>
  )
}

import type { ReactNode } from 'react'

/** Scrolling container for one console screen. */
export function SutrPage({ children }: { children: ReactNode }) {
  return <div className="sutr-page">{children}</div>
}

export function SutrPageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
  children,
}: {
  eyebrow?: ReactNode
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  /** Rendered below the heading row — tabs, filters, a toolbar. */
  children?: ReactNode
}) {
  return (
    <div style={{ borderBottom: '1px solid var(--border)' }}>
      <div className="sutr-page__header" style={{ borderBottom: 'none' }}>
        <div className="sutr-page__heading">
          {eyebrow ? <span className="sutr-eyebrow">{eyebrow}</span> : null}
          <h1 className="sutr-page__title">{title}</h1>
          {subtitle ? <p className="sutr-page__subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="sutr-page__actions">{actions}</div> : null}
      </div>
      {children ? <div className="sutr-page__header-extra">{children}</div> : null}
    </div>
  )
}

export function SutrPageBody({ children }: { children: ReactNode }) {
  return <div className="sutr-page__body">{children}</div>
}

export function SutrSectionLabel({
  children,
  action,
}: {
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
      }}
    >
      <span className="sutr-section-label">{children}</span>
      {action}
    </div>
  )
}

export function SutrDefinitionList({ items }: { items: { key: string; value: ReactNode }[] }) {
  return (
    <dl className="sutr-dl">
      {items.map((item) => (
        <div key={item.key} style={{ display: 'contents' }}>
          <dt className="sutr-dl__key">{item.key}</dt>
          <dd className="sutr-dl__val" style={{ margin: 0 }}>
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

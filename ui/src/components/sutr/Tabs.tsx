import type { ReactNode } from 'react'

export interface TabItem<T extends string> {
  value: T
  label: ReactNode
  count?: number
}

/** Roving-tabindex tab strip. Arrow keys move, Enter/Space is implicit. */
export function SutrTabs<T extends string>({
  items,
  value,
  onChange,
  ariaLabel,
}: {
  items: TabItem<T>[]
  value: T
  onChange: (next: T) => void
  ariaLabel: string
}) {
  function onKeyDown(e: React.KeyboardEvent) {
    const index = items.findIndex((i) => i.value === value)
    if (index < 0) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const delta = e.key === 'ArrowRight' ? 1 : -1
      const next = items[(index + delta + items.length) % items.length]
      onChange(next.value)
    }
  }

  return (
    <div className="sutr-tabs" role="tablist" aria-label={ariaLabel} onKeyDown={onKeyDown}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          className="sutr-tab"
          aria-selected={item.value === value}
          tabIndex={item.value === value ? 0 : -1}
          onClick={() => onChange(item.value)}
        >
          {item.label}
          {typeof item.count === 'number' ? (
            <span className="sutr-tab__count">{item.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  )
}

/** Compact switch between equivalent views of the same data. */
export function SutrSegmented<T extends string>({
  items,
  value,
  onChange,
  ariaLabel,
}: {
  items: { value: T; label: ReactNode }[]
  value: T
  onChange: (next: T) => void
  ariaLabel: string
}) {
  return (
    <div className="sutr-segment" role="group" aria-label={ariaLabel}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          className="sutr-segment__item"
          aria-pressed={item.value === value}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

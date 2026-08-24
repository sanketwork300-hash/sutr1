import type { ReactNode } from 'react'
import type { Tone } from './status'

export interface TimelineEntry {
  id: string
  /** Pre-formatted clock time — the caller decides the precision it needs. */
  time: string
  title: ReactNode
  detail?: ReactNode
  tone?: Extract<Tone, 'success' | 'warning' | 'danger' | 'brand'> | 'neutral'
}

/**
 * A vertical spine of events. Used for activity, execution traces and
 * deployment lifecycles — anywhere ordering carries meaning that a table's
 * rows would flatten away.
 */
export function SutrTimeline({
  entries,
  onSelect,
}: {
  entries: TimelineEntry[]
  onSelect?: (entry: TimelineEntry) => void
}) {
  return (
    <div className="sutr-timeline">
      {entries.map((entry) => {
        const inner = (
          <>
            <span className="sutr-timeline__time">{entry.time}</span>
            <span className="sutr-timeline__spine" aria-hidden="true">
              <span
                className={`sutr-timeline__node${
                  entry.tone && entry.tone !== 'neutral'
                    ? ` sutr-timeline__node--${entry.tone}`
                    : ''
                }`}
              />
            </span>
            <span className="sutr-timeline__body">
              <span className="sutr-timeline__title">{entry.title}</span>
              {entry.detail ? <span className="sutr-meta">{entry.detail}</span> : null}
            </span>
          </>
        )

        if (!onSelect) {
          return (
            <div key={entry.id} className="sutr-timeline__item">
              {inner}
            </div>
          )
        }

        return (
          <button
            key={entry.id}
            type="button"
            className="sutr-timeline__item"
            onClick={() => onSelect(entry)}
            style={{
              appearance: 'none',
              border: 'none',
              background: 'none',
              font: 'inherit',
              textAlign: 'left',
              cursor: 'pointer',
              width: '100%',
              borderRadius: 'var(--r-sm)',
            }}
          >
            {inner}
          </button>
        )
      })}
    </div>
  )
}

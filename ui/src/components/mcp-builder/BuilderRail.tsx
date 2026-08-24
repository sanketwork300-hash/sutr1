import { AlertTriangle, Check } from 'lucide-react'
import type { ReactNode } from 'react'

export interface RailStage<T extends string> {
  id: T
  label: string
}

/**
 * The pipeline, horizontally. A completed stage is navigable; a stage ahead of
 * the pipeline is not, because its inputs do not exist yet.
 */
export function BuilderRail<T extends string>({
  stages,
  current,
  onJump,
}: {
  stages: RailStage<T>[]
  current: T
  onJump: (stage: T) => void
}) {
  const currentIndex = stages.findIndex((stage) => stage.id === current)

  return (
    <nav className="mb__rail" aria-label="Builder stages">
      {stages.map((stage, index) => {
        const done = index < currentIndex
        const active = index === currentIndex
        return (
          <button
            key={stage.id}
            type="button"
            className="mb__stage"
            data-done={done}
            data-active={active}
            aria-current={active ? 'step' : undefined}
            disabled={!done && !active}
            onClick={() => done && onJump(stage.id)}
          >
            <span className="mb__stage-index">
              {done ? <Check size={10} style={{ color: 'var(--green)' }} /> : null}
              {String(index + 1).padStart(2, '0')}
            </span>
            <span className="mb__stage-label">{stage.label}</span>
          </button>
        )
      })}
    </nav>
  )
}

/**
 * What the pipeline has produced so far — the counts an operator checks before
 * committing to a build, kept visible while the stage body scrolls.
 */
export function BuilderSummary({
  rows,
  warnings,
  error,
  footer,
}: {
  rows: { key: string; value: ReactNode }[]
  warnings?: number
  error?: string | null
  footer?: ReactNode
}) {
  return (
    <div className="mb__summary">
      <span className="sutr-section-label">Build summary</span>

      {rows.map((row) => (
        <div key={row.key} className="mb__summary-row">
          <span className="mb__summary-key">{row.key}</span>
          <span
            className="mb__summary-val"
            title={typeof row.value === 'string' ? row.value : undefined}
          >
            {row.value}
          </span>
        </div>
      ))}

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          paddingTop: 8,
          borderTop: '1px solid var(--border)',
        }}
      >
        {error ? (
          <>
            <AlertTriangle size={12} style={{ color: 'var(--red)' }} />
            <span className="sutr-meta" style={{ color: 'var(--red)' }}>
              1 error
            </span>
          </>
        ) : (
          <span className="sutr-meta">0 errors</span>
        )}
        <span className="sutr-meta" style={{ marginLeft: 'auto' }}>
          {warnings ?? 0} warning{(warnings ?? 0) === 1 ? '' : 's'}
        </span>
      </div>

      {footer}
    </div>
  )
}

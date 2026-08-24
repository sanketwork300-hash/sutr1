import { useId, useState } from 'react'

export interface Point {
  /** ISO date, used as the key and for the axis labels. */
  date: string
  label: string
  value: number
}

/**
 * A single-series time bar chart.
 *
 * One measure, one hue, one axis — no second scale, no legend (the title names
 * the series), no number printed on every bar. Hovering a bar gives the exact
 * value; a table view of the same numbers is one click away, so the reading
 * never depends on colour or on hover being available.
 */
export function SutrBarChart({
  points,
  title,
  unit = '',
  height = 132,
}: {
  points: Point[]
  title: string
  unit?: string
  height?: number
}) {
  const [hover, setHover] = useState<number | null>(null)
  const [asTable, setAsTable] = useState(false)
  const tableId = useId()

  const max = Math.max(1, ...points.map((p) => p.value))
  const gap = 2
  const width = 720
  const plot = height - 22
  const barWidth = points.length > 0 ? Math.max(2, width / points.length - gap) : 0

  const total = points.reduce((sum, p) => sum + p.value, 0)

  return (
    <figure style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
      <figcaption
        style={{ display: 'flex', alignItems: 'center', gap: 10, justifyContent: 'space-between' }}
      >
        <span className="sutr-section-label">{title}</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="sutr-meta sutr-mono">
            {total.toLocaleString()}
            {unit ? ` ${unit}` : ''} · peak {max.toLocaleString()}
          </span>
          <button
            type="button"
            className="sutr-btn sutr-btn--ghost sutr-btn--sm"
            aria-expanded={asTable}
            aria-controls={tableId}
            onClick={() => setAsTable((v) => !v)}
          >
            {asTable ? 'Chart' : 'Table'}
          </button>
        </span>
      </figcaption>

      {asTable ? (
        <div className="sutr-table-wrap" id={tableId} style={{ maxHeight: 260, overflowY: 'auto' }}>
          <table className="sutr-table" style={{ minWidth: 240 }}>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col" style={{ textAlign: 'right' }}>
                  {title}
                </th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.date}>
                  <td>{point.label}</td>
                  <td className="sutr-table__num">{point.value.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div style={{ position: 'relative' }}>
          <svg
            viewBox={`0 0 ${width} ${height}`}
            width="100%"
            height={height}
            role="img"
            aria-label={`${title}: ${total.toLocaleString()} across ${points.length} days, peak ${max.toLocaleString()} on ${points.find((p) => p.value === max)?.label ?? 'the busiest day'}.`}
            style={{ display: 'block', overflow: 'visible' }}
            onMouseLeave={() => setHover(null)}
          >
            {/* Recessive reference lines: the ceiling and the baseline only. */}
            <line x1="0" y1="0.5" x2={width} y2="0.5" stroke="var(--chart-grid)" strokeWidth="1" />
            <line
              x1="0"
              y1={plot + 0.5}
              x2={width}
              y2={plot + 0.5}
              stroke="var(--border-strong)"
              strokeWidth="1"
            />

            {points.map((point, index) => {
              const barHeight = point.value === 0 ? 0 : Math.max(2, (point.value / max) * plot)
              const x = index * (barWidth + gap)
              return (
                <g key={point.date}>
                  {/* Full-height hit target: hovering a short bar should not
                      require pixel accuracy. */}
                  <rect
                    x={x}
                    y={0}
                    width={barWidth + gap}
                    height={plot}
                    fill="transparent"
                    onMouseEnter={() => setHover(index)}
                  />
                  <rect
                    x={x}
                    y={plot - barHeight}
                    width={barWidth}
                    height={barHeight}
                    rx={Math.min(3, barWidth / 2)}
                    fill="var(--chart-1)"
                    opacity={hover === null || hover === index ? 1 : 0.45}
                    pointerEvents="none"
                  />
                </g>
              )
            })}

            {points.length > 0 ? (
              <>
                <text x={0} y={height - 6} fontSize="10" fill="var(--text-faint)">
                  {points[0].label}
                </text>
                <text
                  x={width}
                  y={height - 6}
                  fontSize="10"
                  textAnchor="end"
                  fill="var(--text-faint)"
                >
                  {points[points.length - 1].label}
                </text>
              </>
            ) : null}
          </svg>

          {hover !== null && points[hover] ? (
            <div
              role="status"
              style={{
                position: 'absolute',
                top: -6,
                left: `clamp(0px, ${((hover + 0.5) / points.length) * 100}%, calc(100% - 140px))`,
                transform: 'translateY(-100%)',
                background: 'var(--surface-elevated)',
                border: '1px solid var(--border-strong)',
                borderRadius: 'var(--r-sm)',
                boxShadow: 'var(--shadow-md)',
                padding: '6px 9px',
                pointerEvents: 'none',
                whiteSpace: 'nowrap',
                zIndex: 2,
              }}
            >
              <span className="sutr-meta" style={{ display: 'block' }}>
                {points[hover].label}
              </span>
              <span className="sutr-mono" style={{ color: 'var(--text)' }}>
                {points[hover].value.toLocaleString()}
                {unit ? ` ${unit}` : ''}
              </span>
            </div>
          ) : null}
        </div>
      )}
    </figure>
  )
}

/**
 * Magnitude across a handful of named things — the bar carries the comparison,
 * the number carries the value, and the label never depends on the bar.
 */
export function SutrBarList({
  items,
  emptyLabel = 'No data in this period.',
}: {
  items: { key: string; label: string; value: number; hint?: string }[]
  emptyLabel?: string
}) {
  if (items.length === 0) return <span className="sutr-meta">{emptyLabel}</span>
  const max = Math.max(1, ...items.map((item) => item.value))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {items.map((item) => (
        <div key={item.key} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <span className="sutr-truncate" style={{ fontSize: 12.5, color: 'var(--text)' }}>
              {item.label}
            </span>
            {item.hint ? <span className="sutr-meta">{item.hint}</span> : null}
            <span
              className="sutr-mono"
              style={{ marginLeft: 'auto', color: 'var(--text-dim)', fontSize: 11.5 }}
            >
              {item.value.toLocaleString()}
            </span>
          </div>
          <div
            style={{
              height: 5,
              borderRadius: 3,
              background: 'var(--neutral-soft)',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${Math.max(2, (item.value / max) * 100)}%`,
                height: '100%',
                borderRadius: 3,
                background: 'var(--chart-1)',
              }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

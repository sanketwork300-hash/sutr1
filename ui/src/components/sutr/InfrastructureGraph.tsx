import { useId } from 'react'

export interface GraphLayer {
  id: string
  label: string
  /** Counts come from the API. `null` renders a dash, never a fabricated 0. */
  value: number | null
  hint?: string
}

/**
 * The shape of the platform, drawn from live numbers: agents reach the Sutr
 * gateway, the gateway governs, tools execute, providers answer.
 *
 * Deliberately not a marketing illustration — the node labels are the same
 * objects the console manages, and the counts are whatever the API returned.
 */
export function SutrInfrastructureGraph({
  layers,
  animate = true,
}: {
  layers: GraphLayer[]
  animate?: boolean
}) {
  const gradientId = useId()
  const rows = layers.slice(0, 4)
  const height = 78 + rows.length * 72

  return (
    <svg
      viewBox={`0 0 480 ${height}`}
      width="100%"
      height="auto"
      role="img"
      aria-label={`Infrastructure path: ${rows.map((r) => r.label).join(' to ')}`}
      style={{ display: 'block', maxHeight: 420 }}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--brand)" stopOpacity="0.9" />
          <stop offset="100%" stopColor="var(--brand)" stopOpacity="0.05" />
        </linearGradient>
      </defs>

      {rows.map((layer, i) => {
        const y = 30 + i * 72
        const isGateway = i === 1
        return (
          <g key={layer.id}>
            {i < rows.length - 1 && (
              <>
                <line
                  x1="240"
                  y1={y + 44}
                  x2="240"
                  y2={y + 72}
                  stroke="var(--border-strong)"
                  strokeWidth="1"
                />
                {animate && (
                  <line
                    x1="240"
                    y1={y + 44}
                    x2="240"
                    y2={y + 72}
                    stroke="var(--brand)"
                    strokeWidth="1.5"
                    strokeDasharray="4 10"
                    style={{ animation: 'sutr-dash 3s linear infinite' }}
                  />
                )}
              </>
            )}

            <rect
              x={isGateway ? 96 : 128}
              y={y}
              width={isGateway ? 288 : 224}
              height="44"
              rx="8"
              fill={isGateway ? 'var(--brand-soft)' : 'var(--surface)'}
              stroke={isGateway ? 'var(--brand-border)' : 'var(--border)'}
            />

            <text
              x={isGateway ? 112 : 144}
              y={y + 19}
              fill="var(--text-faint)"
              fontSize="9"
              fontWeight="600"
              letterSpacing="1.2"
              style={{ textTransform: 'uppercase' }}
            >
              {layer.label.toUpperCase()}
            </text>
            <text
              x={isGateway ? 112 : 144}
              y={y + 34}
              fill="var(--text)"
              fontSize="12"
              fontWeight="500"
            >
              {layer.hint ?? ''}
            </text>
            <text
              x={isGateway ? 368 : 336}
              y={y + 28}
              textAnchor="end"
              fill={isGateway ? 'var(--brand)' : 'var(--text-dim)'}
              fontSize="15"
              fontWeight="600"
              fontFamily="var(--font-mono)"
            >
              {layer.value === null ? '—' : layer.value.toLocaleString()}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

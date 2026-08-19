import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { api, type UsageSummary } from '@/api/client'

const RANGES = [
  { label: '7 days', days: 7 },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
] as const

export default function UsagePage() {
  const [days, setDays] = useState<number>(30)
  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (windowDays: number) => {
    setLoading(true)
    try {
      const start = new Date(Date.now() - windowDays * 86400_000).toISOString()
      setSummary(await api.usage.summary({ start }))
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load usage')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(days)
  }, [days, load])

  const executed =
    summary?.tool_calls_by_outcome.find((row) => row.outcome === 'executed')?.count ?? 0
  const errored = summary?.tool_calls_by_outcome.find((row) => row.outcome === 'error')?.count ?? 0
  const deploymentMinutes =
    summary?.totals_by_kind.find((row) => row.kind === 'deployment_runtime')?.quantity ?? 0
  const errorRate = summary && summary.tool_calls > 0 ? (errored / summary.tool_calls) * 100 : 0

  return (
    <div style={{ flex: 1, overflow: 'auto', background: 'var(--bg)', padding: '28px 24px 80px' }}>
      <div style={{ maxWidth: 860, margin: '0 auto' }}>
        {/* Filters sit in one row above the charts */}
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-end',
            justifyContent: 'space-between',
            gap: 12,
            marginBottom: 18,
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: 'var(--text)' }}>
              Usage
            </h1>
            <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-dim)' }}>
              Metered tool calls and deployment runtime for your organization.
            </p>
          </div>
          <div style={{ display: 'flex', gap: 4 }}>
            {RANGES.map((range) => (
              <button
                key={range.days}
                type="button"
                onClick={() => setDays(range.days)}
                aria-pressed={days === range.days}
                style={{
                  padding: '5px 10px',
                  fontSize: 12,
                  borderRadius: 6,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  border: `1px solid ${days === range.days ? 'var(--text)' : 'var(--border)'}`,
                  background: days === range.days ? 'var(--content-bg)' : 'var(--surface)',
                  color: days === range.days ? 'var(--text)' : 'var(--text-dim)',
                  fontWeight: days === range.days ? 600 : 400,
                }}
              >
                {range.label}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div
            style={{
              marginBottom: 14,
              padding: '8px 12px',
              borderRadius: 8,
              fontSize: 12.5,
              background: 'var(--badge-red-bg)',
              color: 'var(--badge-red-text)',
            }}
          >
            {error}
          </div>
        )}

        {loading && !summary ? (
          <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-faint)' }}>
            <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} />
          </div>
        ) : summary ? (
          <>
            {/* Headline numbers are stat tiles, not charts. */}
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
                gap: 10,
                marginBottom: 16,
              }}
            >
              <StatTile label="Tool calls" value={summary.tool_calls.toLocaleString()} />
              <StatTile label="Succeeded" value={executed.toLocaleString()} />
              <StatTile
                label="Errors"
                value={errored.toLocaleString()}
                caption={summary.tool_calls > 0 ? `${errorRate.toFixed(1)}% of calls` : undefined}
                status={errored > 0 ? 'warning' : undefined}
              />
              <StatTile
                label="Avg latency"
                value={
                  summary.duration_ms.avg !== null ? `${Math.round(summary.duration_ms.avg)} ms` : '—'
                }
                caption={
                  summary.duration_ms.max !== null
                    ? `max ${summary.duration_ms.max.toLocaleString()} ms`
                    : undefined
                }
              />
              <StatTile
                label="Deployment runtime"
                value={formatMinutes(deploymentMinutes)}
                caption="metered in 5-min samples"
              />
            </div>

            <DailyCalls daily={summary.daily} />

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                gap: 16,
                marginTop: 16,
              }}
            >
              <RankedTable
                title="Top tools"
                rows={summary.top_tools.map((row) => ({
                  key: `${row.integration_id}/${row.tool_name}`,
                  label: row.tool_name ?? '—',
                  sub: row.integration_id ?? undefined,
                  count: row.count,
                }))}
              />
              <RankedTable
                title="By integration"
                rows={summary.top_integrations.map((row) => ({
                  key: row.integration_id ?? 'unknown',
                  label: row.integration_id ?? '—',
                  count: row.count,
                }))}
                extra={
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', paddingTop: 10 }}>
                    {summary.tool_calls_by_source.map((row) => (
                      <span key={row.source ?? 'unknown'} style={chipStyle}>
                        {row.source ?? 'unknown'} · {row.count.toLocaleString()}
                      </span>
                    ))}
                  </div>
                }
              />
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}

/** One series, so no legend: the title names it. Hover gives per-bar detail. */
function DailyCalls({ daily }: { daily: { date: string; count: number }[] }) {
  const [hovered, setHovered] = useState<number | null>(null)
  const max = daily.reduce((acc, row) => Math.max(acc, row.count), 0)
  const active = hovered !== null ? daily[hovered] : null

  return (
    <section
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        padding: '14px 16px 10px',
      }}
    >
      <div
        style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}
      >
        <h2 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
          Tool calls per day
        </h2>
        <span
          style={{
            fontSize: 11.5,
            color: active ? 'var(--text)' : 'var(--text-faint)',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {active
            ? `${active.date} · ${active.count.toLocaleString()} call${active.count === 1 ? '' : 's'}`
            : max > 0
              ? `peak ${max.toLocaleString()}`
              : 'no calls yet'}
        </span>
      </div>

      {daily.length === 0 ? (
        <p style={{ fontSize: 12.5, color: 'var(--text-faint)', margin: '18px 0' }}>
          No tool calls in this window.
        </p>
      ) : (
        <>
          <div
            role="img"
            aria-label={`Tool calls per day. Peak ${max} on a single day across ${daily.length} days with activity.`}
            style={{
              display: 'flex',
              alignItems: 'flex-end',
              gap: 2, // 2px surface gap between adjacent bars
              height: 120,
              marginTop: 12,
              borderBottom: '1px solid var(--border)',
              paddingBottom: 1,
            }}
            onMouseLeave={() => setHovered(null)}
          >
            {daily.map((row, index) => {
              const ratio = max > 0 ? row.count / max : 0
              return (
                <div
                  key={row.date}
                  onMouseEnter={() => setHovered(index)}
                  title={`${row.date}: ${row.count} call${row.count === 1 ? '' : 's'}`}
                  style={{
                    flex: 1,
                    minWidth: 3,
                    height: '100%',
                    display: 'flex',
                    alignItems: 'flex-end',
                    cursor: 'default',
                  }}
                >
                  <div
                    style={{
                      width: '100%',
                      // Anchored to the baseline; a floor so a 1-call day is visible.
                      height: `${Math.max(ratio * 100, row.count > 0 ? 3 : 0)}%`,
                      background: 'var(--chart-1)',
                      borderRadius: '4px 4px 0 0', // rounded data-end only
                      opacity: hovered === null || hovered === index ? 1 : 0.45,
                      transition: 'opacity 120ms ease',
                    }}
                  />
                </div>
              )
            })}
          </div>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontSize: 10.5,
              color: 'var(--text-faint)',
              paddingTop: 5,
            }}
          >
            <span>{daily[0].date}</span>
            {daily.length > 1 && <span>{daily[daily.length - 1].date}</span>}
          </div>
        </>
      )}
    </section>
  )
}

function StatTile({
  label,
  value,
  caption,
  status,
}: {
  label: string
  value: string
  caption?: string
  status?: 'warning'
}) {
  return (
    <div
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        padding: '12px 14px',
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          color: 'var(--text-faint)',
          display: 'flex',
          alignItems: 'center',
          gap: 4,
        }}
      >
        {/* Status carries an icon + label, never colour alone. */}
        {status === 'warning' && (
          <AlertTriangle size={11} style={{ color: 'var(--badge-amber-dot)' }} />
        )}
        {label}
      </div>
      <div
        style={{
          fontSize: 22,
          fontWeight: 600,
          color: 'var(--text)',
          fontVariantNumeric: 'tabular-nums',
          lineHeight: 1.2,
          marginTop: 4,
        }}
      >
        {value}
      </div>
      {caption && (
        <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 2 }}>{caption}</div>
      )}
    </div>
  )
}

/** Ranked magnitude as a table with inline bars — doubles as the table view. */
function RankedTable({
  title,
  rows,
  extra,
}: {
  title: string
  rows: { key: string; label: string; sub?: string; count: number }[]
  extra?: React.ReactNode
}) {
  const max = rows.reduce((acc, row) => Math.max(acc, row.count), 0)
  return (
    <section
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        padding: '14px 16px',
      }}
    >
      <h2 style={{ margin: '0 0 10px', fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
        {title}
      </h2>
      {rows.length === 0 ? (
        <p style={{ fontSize: 12.5, color: 'var(--text-faint)', margin: 0 }}>No data yet.</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <tbody>
            {rows.slice(0, 8).map((row) => (
              <tr key={row.key}>
                <td style={{ padding: '4px 0', fontSize: 12.5, color: 'var(--text)' }}>
                  <span style={{ fontFamily: 'var(--font-mono)' }}>{row.label}</span>
                  {row.sub && (
                    <span style={{ color: 'var(--text-faint)', fontSize: 11 }}> · {row.sub}</span>
                  )}
                </td>
                <td style={{ width: '38%', padding: '4px 0 4px 10px' }}>
                  <div style={{ background: 'var(--chart-1-soft)', height: 6, borderRadius: 3 }}>
                    <div
                      style={{
                        width: `${max > 0 ? (row.count / max) * 100 : 0}%`,
                        height: 6,
                        background: 'var(--chart-1)',
                        borderRadius: 3,
                      }}
                    />
                  </div>
                </td>
                <td
                  style={{
                    width: 52,
                    textAlign: 'right',
                    fontSize: 12,
                    color: 'var(--text-dim)',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {row.count.toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {extra}
    </section>
  )
}

function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const hours = minutes / 60
  if (hours < 48) return `${hours.toFixed(1)} h`
  return `${(hours / 24).toFixed(1)} d`
}

const chipStyle: React.CSSProperties = {
  fontSize: 11,
  padding: '2px 8px',
  borderRadius: 999,
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  color: 'var(--text-dim)',
  fontFamily: 'var(--font-mono)',
}

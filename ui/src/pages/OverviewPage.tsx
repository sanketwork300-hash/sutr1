import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity as ActivityIcon,
  Boxes,
  Plug,
  Rocket,
  ShieldQuestion,
  Wrench,
  Zap,
} from 'lucide-react'
import { api, type LogEntry, type UsageSummary } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspace'
import { currentEnvironment } from '@/components/shell/environment'
import { formatClock, displayName, greeting, relativeTime } from '@/lib/format'
import {
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrError,
  SutrInfrastructureGraph,
  SutrLinkButton,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrStatus,
  SutrTimeline,
  describeError,
  type TimelineEntry,
} from '@/components/sutr'

/**
 * Infrastructure overview, not a welcome screen. Every number is read from the
 * API; anything that has not loaded shows a skeleton, and anything that is
 * genuinely zero says zero.
 */
export default function OverviewPage() {
  const email = useAuthStore((s) => s.email)
  const { org, workspaces, activeId, load: loadWorkspace } = useWorkspaceStore()
  const {
    tools,
    installed,
    deployments,
    projects,
    pendingApprovals,
    loaded,
    error,
    load,
    refresh,
  } = useCatalogStore()

  const [logs, setLogs] = useState<LogEntry[] | null>(null)
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [feedError, setFeedError] = useState<string | null>(null)

  useEffect(() => {
    void load()
    void loadWorkspace()
  }, [load, loadWorkspace])

  useEffect(() => {
    let cancelled = false
    async function read() {
      try {
        const [entries, summary] = await Promise.all([
          api.logs.list({ limit: 8 }),
          api.usage.summary(),
        ])
        if (cancelled) return
        setLogs(entries)
        setUsage(summary)
      } catch (err) {
        if (!cancelled) setFeedError(describeError(err).message)
      }
    }
    void read()
    return () => {
      cancelled = true
    }
  }, [])

  const running = deployments.filter((d) => d.status === 'running')
  const workspace = workspaces.find((w) => w.id === activeId)
  const environment = currentEnvironment()

  const timeline: TimelineEntry[] = (logs ?? []).map((entry) => ({
    id: String(entry.id),
    time: formatClock(entry.timestamp),
    tone:
      entry.outcome === 'denied' || entry.outcome === 'error'
        ? 'danger'
        : entry.outcome === 'approval_required'
          ? 'warning'
          : 'success',
    title: (
      <>
        <code className="sutr-mono" style={{ color: 'var(--text)' }}>
          {entry.tool_name}
        </code>
        <SutrStatus domain="outcome" value={entry.outcome} />
      </>
    ),
    detail: `${entry.integration_id}${entry.duration_ms ? ` · ${entry.duration_ms} ms` : ''} · ${relativeTime(entry.timestamp)}`,
  }))

  return (
    <SutrPage>
      <div className="sutr-page__header">
        <div className="sutr-page__heading">
          <span className="sutr-eyebrow">
            {org?.name ?? 'Organisation'} · {workspace?.name ?? 'Default'} · {environment}
          </span>
          <h1 className="sutr-page__title">
            {greeting()}, {displayName(email)}
          </h1>
          <p className="sutr-page__subtitle">
            Everything your agents can reach, and everything they have done with it.
          </p>
        </div>
        <div className="sutr-page__actions">
          <SutrLinkButton to="/app/apis/new" variant="brand" size="sm">
            <Zap size={13} /> New MCP server
          </SutrLinkButton>
        </div>
      </div>

      <SutrPageBody>
        {error ? (
          <SutrError
            what="Part of the workspace inventory could not be read."
            why={error}
            action={
              <button
                type="button"
                className="sutr-btn sutr-btn--secondary sutr-btn--sm"
                onClick={() => void refresh()}
              >
                Retry
              </button>
            }
          />
        ) : null}

        <div className="sutr-grid sutr-grid--metrics">
          <SutrMetric
            label="MCP servers running"
            value={loaded ? running.length : null}
            icon={<Boxes size={12} />}
            to="/app/deployments"
            muted={loaded && running.length === 0}
            foot={loaded ? `${deployments.length} total deployments` : undefined}
          />
          <SutrMetric
            label="Integrations"
            value={loaded ? installed.length : null}
            icon={<Plug size={12} />}
            to="/app/integrations"
            muted={loaded && installed.length === 0}
            foot={loaded ? `${installed.filter((i) => i.connected).length} connected` : undefined}
          />
          <SutrMetric
            label="Tools exposed"
            value={loaded ? tools.length : null}
            icon={<Wrench size={12} />}
            to="/app/tools"
            muted={loaded && tools.length === 0}
            foot={loaded ? `${projects.length} API specifications` : undefined}
          />
          <SutrMetric
            label="Pending approvals"
            value={loaded ? pendingApprovals.length : null}
            icon={<ShieldQuestion size={12} />}
            to="/app/approvals"
            muted={loaded && pendingApprovals.length === 0}
            foot={
              loaded && pendingApprovals.length > 0 ? 'Awaiting a human decision' : 'Queue is clear'
            }
          />
          <SutrMetric
            label="Tool executions"
            value={usage ? usage.tool_calls : null}
            icon={<ActivityIcon size={12} />}
            to="/app/usage"
            muted={Boolean(usage) && usage?.tool_calls === 0}
            foot={usage ? 'Current billing period' : undefined}
          />
          <SutrMetric
            label="Average latency"
            value={
              usage
                ? usage.duration_ms.avg === null
                  ? 'No data'
                  : `${Math.round(usage.duration_ms.avg)} ms`
                : null
            }
            icon={<Rocket size={12} />}
            to="/app/usage"
            foot={
              usage?.duration_ms.max !== null && usage?.duration_ms.max !== undefined
                ? `peak ${Math.round(usage.duration_ms.max)} ms`
                : undefined
            }
          />
        </div>

        <div className="sutr-split-main">
          <SutrCard>
            <SutrCardHeader
              title="Recent activity"
              meta="The last executions to pass through the gateway"
              actions={
                <Link to="/app/activity" className="sutr-btn sutr-btn--ghost sutr-btn--sm">
                  View all
                </Link>
              }
            />
            <SutrCardBody>
              {feedError ? (
                <SutrError what="Activity could not be loaded." why={feedError} />
              ) : logs === null ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {[0, 1, 2, 3].map((i) => (
                    <span key={i} className="sutr-skeleton" style={{ height: 28 }} />
                  ))}
                </div>
              ) : timeline.length === 0 ? (
                <SutrEmpty
                  icon={<ActivityIcon size={17} />}
                  title="No tool executions yet"
                  body="Once an agent calls a tool through this instance, it appears here with its policy decision, latency and outcome."
                  action={
                    <SutrLinkButton to="/app/playground" variant="secondary" size="sm">
                      Open the playground
                    </SutrLinkButton>
                  }
                />
              ) : (
                <SutrTimeline entries={timeline} />
              )}
            </SutrCardBody>
          </SutrCard>

          <SutrCard>
            <SutrCardHeader title="Infrastructure map" meta="Live counts from this workspace" />
            <SutrCardBody>
              <SutrInfrastructureGraph
                layers={[
                  {
                    id: 'agents',
                    label: 'Agents',
                    value: usage ? usage.tool_calls : null,
                    hint: 'calls this period',
                  },
                  {
                    id: 'gateway',
                    label: 'Sutr gateway',
                    value: loaded ? pendingApprovals.length : null,
                    hint: 'awaiting approval',
                  },
                  {
                    id: 'tools',
                    label: 'Tools',
                    value: loaded ? tools.length : null,
                    hint: 'governed capabilities',
                  },
                  {
                    id: 'providers',
                    label: 'Providers',
                    value: loaded ? installed.length + running.length : null,
                    hint: 'integrations and MCP servers',
                  },
                ]}
              />
            </SutrCardBody>
          </SutrCard>
        </div>
      </SutrPageBody>
    </SutrPage>
  )
}

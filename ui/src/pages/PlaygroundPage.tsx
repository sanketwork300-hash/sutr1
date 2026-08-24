import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import { Play, Plug, X } from 'lucide-react'
import './playground.css'
import { api, type InstalledIntegration, type Tool } from '@/api/client'
import { useConnectionsStore } from '@/stores/connections'
import { ModeControl } from '@/components/playground/ModeControl'
import { SchemaForm } from '@/components/playground/SchemaForm'
import { ResponsePanel, type PlaygroundResult } from '@/components/playground/ResponsePanel'
import ApprovePage from '@/pages/ApprovePage'
import { useIsMobile } from '@/lib/useMediaQuery'
import {
  SutrBadge,
  SutrButton,
  SutrEmpty,
  SutrKbd,
  SutrLinkButton,
  SutrPageHeader,
  SutrSearchInput,
  SutrSegmented,
  SutrSelect,
  SutrSpinner,
} from '@/components/sutr'

/**
 * A developer console, not a form: choose a tool on the left, shape its
 * arguments in the middle, read the execution on the right — status, latency
 * and the raw result the provider returned.
 *
 * The execution path is unchanged: the same `/api/tools/{id}/call` endpoint,
 * the same approval gate, the same embedded approval drawer when a tool's
 * policy asks for a human.
 */
export default function PlaygroundPage() {
  const { installed, integrations, fetchInstalled, fetchIntegrations } = useConnectionsStore()
  const isMobile = useIsMobile()
  const [params, setParams] = useSearchParams()

  const [selectedIntegration, setSelectedIntegration] = useState<InstalledIntegration | null>(null)
  const [tools, setTools] = useState<Tool[]>([])
  const [toolsLoading, setToolsLoading] = useState(false)
  const [selectedTool, setSelectedTool] = useState<Tool | null>(null)
  const [toolQuery, setToolQuery] = useState('')

  const [fieldValues, setFieldValues] = useState<Record<string, unknown>>({})
  const [rawMode, setRawMode] = useState(false)
  const [rawJson, setRawJson] = useState('{}')

  const [running, setRunning] = useState(false)
  const [awaitingApproval, setAwaitingApproval] = useState(false)
  const [result, setResult] = useState<PlaygroundResult | null>(null)
  const [durationMs, setDurationMs] = useState<number | null>(null)
  const lastArgsRef = useRef<Record<string, unknown>>({})
  const doCallRef = useRef<(args: Record<string, unknown>) => Promise<void>>(async () => {})

  const [drawerOpen, setDrawerOpen] = useState(false)
  const approvalRequestId =
    result?.status === 'approval_required' ? result.data.approval_request_id : null

  useEffect(() => {
    if (installed.length === 0) fetchInstalled()
    if (integrations.length === 0) fetchIntegrations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A deep link from the tool catalog wins over "first installed integration".
  useEffect(() => {
    if (installed.length === 0 || selectedIntegration) return
    const requested = params.get('integration')
    const match = requested
      ? installed.find((entry) => entry.integration_id === requested)
      : undefined
    setSelectedIntegration(match ?? installed[0])
  }, [installed, params, selectedIntegration])

  useEffect(() => {
    if (!selectedIntegration) {
      setTools([])
      return
    }
    setToolsLoading(true)
    setSelectedTool(null)
    setFieldValues({})
    setRawJson('{}')
    setRawMode(false)
    setResult(null)
    setDurationMs(null)
    const requestedTool = params.get('tool')
    api.tools
      .listForIntegration(selectedIntegration.integration_id)
      .then((fetched) => {
        setTools(fetched)
        const target = requestedTool ? fetched.find((t) => t.name === requestedTool) : undefined
        if (target ?? fetched[0]) setSelectedTool(target ?? fetched[0])
      })
      .catch(() => setTools([]))
      .finally(() => setToolsLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIntegration?.integration_id])

  useEffect(() => {
    if (!selectedTool) return
    setFieldValues(initFieldValues(selectedTool))
    setRawJson('{}')
    setRawMode(false)
    setResult(null)
    setDurationMs(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTool?.name])

  // Poll while an approval is outstanding, then re-issue the identical call.
  useEffect(() => {
    if (!awaitingApproval || !approvalRequestId) return
    const interval = setInterval(async () => {
      try {
        const request = await api.approvals.get(approvalRequestId)
        if (request.status === 'approved' || request.status === 'consumed') {
          clearInterval(interval)
          setAwaitingApproval(false)
          setDrawerOpen(false)
          await doCallRef.current(lastArgsRef.current)
        } else if (request.status === 'denied' || request.status === 'expired') {
          clearInterval(interval)
          setAwaitingApproval(false)
          setResult({
            status: 'denied',
            data: { error: 'denied', message: `The approval request was ${request.status}.` },
          })
        }
      } catch {
        // Transient failures are expected while a decision is outstanding.
      }
    }, 2000)
    return () => clearInterval(interval)
  }, [awaitingApproval, approvalRequestId])

  function initFieldValues(tool: Tool): Record<string, unknown> {
    const props =
      (tool.inputSchema?.properties as Record<string, { type?: string }> | undefined) ?? {}
    const values: Record<string, unknown> = {}
    for (const [key, prop] of Object.entries(props)) {
      values[key] = prop.type === 'boolean' ? false : ''
    }
    return values
  }

  function buildArgs(): Record<string, unknown> | null {
    if (rawMode) {
      try {
        return rawJson.trim() ? JSON.parse(rawJson) : {}
      } catch {
        return null
      }
    }
    const schema = selectedTool?.inputSchema
    const props = schema?.properties as Record<string, { type?: string }> | undefined
    if (!props || Object.keys(props).length === 0) return {}

    const args: Record<string, unknown> = {}
    for (const [key, prop] of Object.entries(props)) {
      const raw = fieldValues[key]
      if (prop.type === 'number' || prop.type === 'integer') {
        if (raw !== '' && raw !== undefined) {
          const parsed = Number(raw)
          if (!Number.isNaN(parsed)) args[key] = parsed
        }
      } else if (prop.type === 'boolean') {
        args[key] = Boolean(raw)
      } else if (prop.type === 'array' || prop.type === 'object') {
        if (typeof raw === 'string' && raw.trim()) {
          try {
            args[key] = JSON.parse(raw)
          } catch {
            return null
          }
        }
      } else if (raw !== '' && raw !== undefined) {
        args[key] = raw
      }
    }
    return args
  }

  async function doCall(args: Record<string, unknown>) {
    if (!selectedIntegration || !selectedTool) return
    setRunning(true)
    const start = Date.now()
    try {
      const response = await api.tools.call(
        selectedIntegration.integration_id,
        selectedTool.name,
        args,
      )
      setDurationMs(Date.now() - start)
      setResult(response)
      if (response.status === 'approval_required') setAwaitingApproval(true)
    } finally {
      setRunning(false)
    }
  }
  doCallRef.current = doCall

  async function handleRun() {
    const args = buildArgs()
    if (args === null) return
    lastArgsRef.current = args
    setAwaitingApproval(false)
    setResult(null)
    setDurationMs(null)
    await doCall(args)
  }

  function handleRawToggle(toRaw: boolean) {
    if (toRaw) setRawJson(JSON.stringify(buildArgs() ?? {}, null, 2))
    setRawMode(toRaw)
  }

  const schemaProps = selectedTool?.inputSchema?.properties as Record<string, unknown> | undefined
  const paramCount = schemaProps ? Object.keys(schemaProps).length : 0

  const hasUnfilledRequired = useMemo(() => {
    if (rawMode) return false
    const schema = selectedTool?.inputSchema
    const required = (schema?.required as string[]) ?? []
    if (required.length === 0) return false
    const props = schema?.properties as Record<string, { type?: string }> | undefined
    return required.some((key) => {
      if (props?.[key]?.type === 'boolean') return false
      const value = fieldValues[key]
      return value === '' || value === undefined || value === null
    })
  }, [rawMode, selectedTool, fieldValues])

  const invalidJson = rawMode && buildArgs() === null
  const canRun = Boolean(
    selectedIntegration && selectedTool && !running && !hasUnfilledRequired && !invalidJson,
  )
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '')

  const handleRunRef = useRef(handleRun)
  handleRunRef.current = handleRun

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && canRun) {
        e.preventDefault()
        void handleRunRef.current()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [canRun])

  const filteredTools = useMemo(() => {
    const needle = toolQuery.trim().toLowerCase()
    if (!needle) return tools
    return tools.filter(
      (tool) =>
        tool.name.toLowerCase().includes(needle) ||
        (tool.description ?? '').toLowerCase().includes(needle),
    )
  }, [tools, toolQuery])

  if (installed.length === 0) {
    return (
      <div className="sutr-page">
        <SutrPageHeader
          eyebrow="Operate"
          title="Playground"
          subtitle="Call a governed tool by hand and read exactly what came back."
        />
        <div className="sutr-page__body">
          <SutrEmpty
            icon={<Plug size={17} />}
            title="No integrations connected"
            body="The playground calls the same governed endpoint your agents use, so it needs at least one connected integration."
            action={
              <SutrLinkButton to="/app/integrations" variant="brand" size="sm">
                Connect an integration
              </SutrLinkButton>
            }
          />
        </div>
      </div>
    )
  }

  return (
    <div className="sutr-page" style={{ overflow: 'hidden' }}>
      <SutrPageHeader
        eyebrow="Operate"
        title="Playground"
        subtitle="The same execution path as an agent: policy, approval, credential injection, audit."
        actions={
          <>
            {selectedTool && selectedIntegration ? (
              <ModeControl
                mode={selectedTool.execution_mode}
                integrationName={selectedIntegration.integration_id}
                toolName={selectedTool.name}
                onModeChange={(next) =>
                  setSelectedTool((prev) => (prev ? { ...prev, execution_mode: next } : prev))
                }
              />
            ) : null}
            <SutrButton
              variant="brand"
              onClick={() => void handleRun()}
              disabled={!canRun}
              loading={running}
            >
              {running ? null : <Play size={13} />}
              {running ? 'Running…' : 'Run'}
              {!running && !isMobile ? <SutrKbd>{isMac ? '⌘↵' : 'Ctrl↵'}</SutrKbd> : null}
            </SutrButton>
          </>
        }
      />

      <div className="pg">
        <section className="pg__pane pg__pane--tools" aria-label="Tool selection">
          <div className="pg__pane-head">
            <span className="pg__pane-title">Tool</span>
            {toolsLoading ? <SutrSpinner size={12} /> : null}
            <span className="sutr-meta" style={{ marginLeft: 'auto' }}>
              {filteredTools.length}
            </span>
          </div>
          <div
            className="pg__pane-body"
            style={{ gap: 8, display: 'flex', flexDirection: 'column' }}
          >
            <SutrSelect
              aria-label="Integration"
              value={selectedIntegration?.integration_id ?? ''}
              onChange={(e) => {
                const next = installed.find((entry) => entry.integration_id === e.target.value)
                setSelectedIntegration(next ?? null)
                params.delete('tool')
                if (next) params.set('integration', next.integration_id)
                setParams(params, { replace: true })
              }}
            >
              {installed.map((entry) => {
                const definition = integrations.find((i) => i.id === entry.integration_id)
                return (
                  <option key={entry.integration_id} value={entry.integration_id}>
                    {definition?.name ?? entry.integration_id}
                  </option>
                )
              })}
            </SutrSelect>

            <SutrSearchInput
              value={toolQuery}
              onValueChange={setToolQuery}
              ariaLabel="Filter tools"
              placeholder="Filter tools"
            />

            <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              {filteredTools.map((tool) => (
                <button
                  key={tool.name}
                  type="button"
                  className="pg__tool"
                  aria-pressed={tool.name === selectedTool?.name}
                  onClick={() => {
                    setSelectedTool(tool)
                    params.set('tool', tool.name)
                    setParams(params, { replace: true })
                  }}
                >
                  <span className="pg__tool-name">{tool.name}</span>
                  {tool.description ? (
                    <span className="sutr-meta sutr-truncate">{tool.description}</span>
                  ) : null}
                </button>
              ))}
              {!toolsLoading && filteredTools.length === 0 ? (
                <span className="sutr-meta" style={{ padding: '8px 10px' }}>
                  No tools match.
                </span>
              ) : null}
            </div>
          </div>
        </section>

        <section className="pg__pane" aria-label="Arguments">
          <div className="pg__pane-head">
            <span className="pg__pane-title">Arguments</span>
            {paramCount > 0 ? (
              <SutrBadge tone="neutral" plain>
                {paramCount}
              </SutrBadge>
            ) : null}
            {hasUnfilledRequired ? (
              <SutrBadge tone="warning" plain>
                required missing
              </SutrBadge>
            ) : null}
            {paramCount > 0 ? (
              <span style={{ marginLeft: 'auto' }}>
                <SutrSegmented
                  ariaLabel="Argument editor mode"
                  value={rawMode ? 'raw' : 'form'}
                  onChange={(mode) => handleRawToggle(mode === 'raw')}
                  items={[
                    { value: 'form', label: 'Form' },
                    { value: 'raw', label: 'JSON' },
                  ]}
                />
              </span>
            ) : null}
          </div>
          <div className="pg__pane-body">
            {selectedTool ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <code className="sutr-mono" style={{ fontSize: 13, color: 'var(--text)' }}>
                    {selectedTool.name}
                  </code>
                  {selectedTool.description ? (
                    <span className="sutr-meta">{selectedTool.description}</span>
                  ) : null}
                </div>

                {paramCount === 0 ? (
                  <span className="sutr-meta">This tool takes no arguments.</span>
                ) : (
                  <SchemaForm
                    schema={selectedTool.inputSchema as Record<string, unknown> | undefined}
                    values={fieldValues}
                    rawMode={rawMode}
                    rawJson={rawJson}
                    onFieldChange={(field, value) =>
                      setFieldValues((prev) => ({ ...prev, [field]: value }))
                    }
                    onRawJsonChange={setRawJson}
                  />
                )}

                {invalidJson ? (
                  <span className="sutr-field__error">
                    The JSON above does not parse, so the call cannot be built.
                  </span>
                ) : null}
              </div>
            ) : (
              <span className="sutr-meta">Select a tool to see its input schema.</span>
            )}
          </div>
        </section>

        <section className="pg__pane pg__pane--result" aria-label="Execution">
          <div className="pg__pane-head">
            <span className="pg__pane-title">Execution</span>
            {durationMs !== null ? (
              <span className="sutr-meta sutr-mono" style={{ marginLeft: 'auto' }}>
                {durationMs} ms
              </span>
            ) : null}
          </div>
          <div className="pg__pane-body">
            <ResponsePanel
              result={result}
              running={running}
              awaitingApproval={awaitingApproval}
              durationMs={durationMs}
              onOpenDrawer={() => setDrawerOpen(true)}
              onCancelPolling={() => setAwaitingApproval(false)}
            />
          </div>
        </section>
      </div>

      {drawerOpen && approvalRequestId
        ? createPortal(
            <>
              <div
                onClick={() => setDrawerOpen(false)}
                style={{ position: 'fixed', inset: 0, background: 'rgba(4,5,6,0.6)', zIndex: 9998 }}
              />
              <div
                style={{
                  position: 'fixed',
                  top: 0,
                  right: 0,
                  bottom: 0,
                  width: isMobile ? '100%' : 500,
                  maxWidth: '100%',
                  background: 'var(--surface-elevated)',
                  borderLeft: '1px solid var(--border-strong)',
                  zIndex: 9999,
                  display: 'flex',
                  flexDirection: 'column',
                  boxShadow: 'var(--shadow-lg)',
                  overflowY: 'auto',
                }}
              >
                <div
                  style={{
                    height: 44,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '0 14px',
                    borderBottom: '1px solid var(--border)',
                    flexShrink: 0,
                    position: 'sticky',
                    top: 0,
                    background: 'var(--surface-elevated)',
                    zIndex: 1,
                  }}
                >
                  <span className="sutr-section-label">Approve tool call</span>
                  <SutrButton
                    variant="ghost"
                    size="sm"
                    iconOnly
                    aria-label="Close"
                    onClick={() => setDrawerOpen(false)}
                  >
                    <X size={14} />
                  </SutrButton>
                </div>
                <ApprovePage
                  overrideId={approvalRequestId}
                  embeddedProp
                  onAllowTool={(integrationName, toolName) => {
                    // Close and re-run immediately rather than waiting for the
                    // next poll; mirror the policy change locally so the mode
                    // control agrees with the server.
                    setDrawerOpen(false)
                    setAwaitingApproval(false)
                    setSelectedTool((prev) =>
                      prev && prev.name === toolName ? { ...prev, execution_mode: 'allow' } : prev,
                    )
                    api.toolSettings.update(integrationName, toolName, 'allow').catch(() => {
                      /* best-effort — the wildcard grant already covers enforcement */
                    })
                    void doCallRef.current(lastArgsRef.current)
                  }}
                />
              </div>
            </>,
            document.body,
          )
        : null}
    </div>
  )
}

import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowRight, FileJson, Globe, KeyRound, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  api,
  type OpenApiCompileResult,
  type OpenApiProject,
  type OpenApiWarning,
} from '@/api/client'

type SourceKind = 'paste' | 'url'

export default function OpenApiImportPage() {
  const navigate = useNavigate()

  // Step 1 — import
  const [name, setName] = useState('')
  const [sourceKind, setSourceKind] = useState<SourceKind>('paste')
  const [content, setContent] = useState('')
  const [url, setUrl] = useState('')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [project, setProject] = useState<OpenApiProject | null>(null)

  // Step 2 — configure
  const [excludedTags, setExcludedTags] = useState<Set<string>>(new Set())
  const [includeDeprecated, setIncludeDeprecated] = useState(false)
  const [serverChoice, setServerChoice] = useState<string>('')
  const [customServerUrl, setCustomServerUrl] = useState('')
  const [serverVariables, setServerVariables] = useState<Record<string, string>>({})
  const [tokenHeader, setTokenHeader] = useState('')
  const [tokenFormat, setTokenFormat] = useState('')
  const [integrationName, setIntegrationName] = useState('')

  // Step 3 — preview / create
  const [preview, setPreview] = useState<OpenApiCompileResult | null>(null)
  const [compiling, setCompiling] = useState(false)
  const [creating, setCreating] = useState(false)
  const [compileError, setCompileError] = useState('')

  const selectedServer = useMemo(() => {
    if (!project?.servers || serverChoice === 'custom') return null
    return project.servers.find((s) => s.url === serverChoice) ?? null
  }, [project, serverChoice])

  async function handleImport(event: { preventDefault: () => void }) {
    event.preventDefault()
    setImportError('')
    if (sourceKind === 'paste' && !content.trim()) {
      setImportError('Paste the OpenAPI document (JSON or YAML)')
      return
    }
    if (sourceKind === 'url' && !url.trim()) {
      setImportError('Enter the specification URL')
      return
    }
    setImporting(true)
    try {
      const created = await api.openapi.import({
        name: name.trim() || undefined,
        source_kind: sourceKind,
        content: sourceKind === 'paste' ? content : undefined,
        url: sourceKind === 'url' ? url.trim() : undefined,
      })
      setProject(created)
      setIntegrationName(created.name)
      setTokenHeader(created.suggested_auth?.token_header ?? '')
      setTokenFormat(created.suggested_auth?.token_format ?? '')
      const firstServer = created.servers?.[0]?.url
      setServerChoice(firstServer ?? 'custom')
      const vars: Record<string, string> = {}
      for (const [key, cfg] of Object.entries(created.servers?.[0]?.variables ?? {})) {
        vars[key] = cfg.default ?? ''
      }
      setServerVariables(vars)
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Import failed')
    } finally {
      setImporting(false)
    }
  }

  function pickServer(next: string) {
    setServerChoice(next)
    setPreview(null)
    const server = project?.servers?.find((s) => s.url === next)
    const vars: Record<string, string> = {}
    for (const [key, cfg] of Object.entries(server?.variables ?? {})) {
      vars[key] = cfg.default ?? ''
    }
    setServerVariables(vars)
  }

  function toggleTag(tag: string) {
    setPreview(null)
    setExcludedTags((prev) => {
      const next = new Set(prev)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  function compileBody(dryRun: boolean) {
    return {
      dry_run: dryRun,
      filters: {
        exclude_tags: [...excludedTags],
        include_deprecated: includeDeprecated,
      },
      server_url: serverChoice === 'custom' ? customServerUrl.trim() : serverChoice,
      server_variables: serverVariables,
      auth:
        tokenHeader.trim() || tokenFormat.trim()
          ? { token_header: tokenHeader.trim(), token_format: tokenFormat }
          : { token_header: '', token_format: '' },
      integration_name: integrationName.trim() || undefined,
    }
  }

  async function handlePreview() {
    if (!project) return
    setCompileError('')
    if (serverChoice === 'custom' && !customServerUrl.trim()) {
      setCompileError('Enter the base URL for the API')
      return
    }
    setCompiling(true)
    try {
      setPreview(await api.openapi.compile(project.id, compileBody(true)))
    } catch (err) {
      setCompileError(err instanceof Error ? err.message : 'Compilation failed')
    } finally {
      setCompiling(false)
    }
  }

  async function handleCreate() {
    if (!project) return
    setCompileError('')
    setCreating(true)
    try {
      const result = await api.openapi.compile(project.id, compileBody(false))
      navigate(`/integrations/custom-api/${result.integration_db_id}`, { replace: true })
    } catch (err) {
      setCompileError(err instanceof Error ? err.message : 'Failed to create the integration')
      setCreating(false)
    }
  }

  return (
    <div style={{ flex: 1, overflow: 'auto', background: 'var(--bg)', padding: '40px 20px 80px' }}>
      <div style={{ maxWidth: 640, margin: '0 auto' }}>
        <button type="button" onClick={() => navigate('/integrations')} style={backLinkStyle}>
          <ArrowLeft size={13} />
          Integrations
        </button>

        {/* ── Step 1: Import ── */}
        <section style={cardStyle}>
          <header style={cardHeaderStyle}>
            <h1 style={cardTitleStyle}>Import an OpenAPI specification</h1>
            <p style={cardSubtitleStyle}>
              Turn any OpenAPI 3.x API into agent tools with the same approval controls as every
              other integration.
            </p>
          </header>

          {!project ? (
            <form onSubmit={handleImport}>
              <div style={{ padding: '20px 28px 8px' }}>
                <Field label="Name" optional hint="Defaults to the API title">
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Petstore"
                    style={inputStyle}
                  />
                </Field>

                <Field label="Source">
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                    <SourceCard
                      icon={<FileJson size={14} />}
                      label="Paste document"
                      hint="JSON or YAML"
                      active={sourceKind === 'paste'}
                      onClick={() => setSourceKind('paste')}
                    />
                    <SourceCard
                      icon={<Globe size={14} />}
                      label="From URL"
                      hint="Publicly reachable spec"
                      active={sourceKind === 'url'}
                      onClick={() => setSourceKind('url')}
                    />
                  </div>
                </Field>

                {sourceKind === 'paste' ? (
                  <Field label="Specification">
                    <textarea
                      value={content}
                      onChange={(e) => setContent(e.target.value)}
                      placeholder={'{\n  "openapi": "3.0.3",\n  ...\n}'}
                      spellCheck={false}
                      style={{
                        ...inputStyle,
                        height: 180,
                        padding: '10px 11px',
                        fontFamily: 'var(--font-mono)',
                        fontSize: 12,
                        resize: 'vertical',
                        lineHeight: 1.5,
                      }}
                    />
                  </Field>
                ) : (
                  <Field label="Specification URL">
                    <input
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      placeholder="https://api.example.com/openapi.json"
                      spellCheck={false}
                      style={{ ...inputStyle, fontFamily: 'var(--font-mono)', fontSize: 12.5 }}
                    />
                  </Field>
                )}
              </div>

              {importError && <ErrorBanner message={importError} />}

              <footer style={cardFooterStyle}>
                <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                  Validated against the official OpenAPI schemas on import.
                </span>
                <Button type="submit" size="sm" disabled={importing}>
                  {importing ? (
                    <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
                  ) : (
                    <span style={{ display: 'inline-flex', alignItems: 'center' }}>
                      Import <ArrowRight size={13} style={{ marginLeft: 6 }} />
                    </span>
                  )}
                </Button>
              </footer>
            </form>
          ) : (
            <div style={{ padding: '16px 28px 20px', display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              <Stat label="API" value={project.api_title} />
              <Stat label="Version" value={project.api_version} />
              <Stat label="OpenAPI" value={project.openapi_version} />
              <Stat label="Operations" value={String(project.operation_count)} />
              {project.warnings.length > 0 && (
                <Stat label="Import warnings" value={String(project.warnings.length)} warn />
              )}
            </div>
          )}
        </section>

        {/* ── Step 2: Configure ── */}
        {project && (
          <section style={{ ...cardStyle, marginTop: 16 }}>
            <header style={cardHeaderStyle}>
              <h2 style={cardTitleStyle}>Configure</h2>
              <p style={cardSubtitleStyle}>
                Choose which operations become tools, where calls go, and how they authenticate.
              </p>
            </header>

            <div style={{ padding: '20px 28px 8px' }}>
              {(project.tags?.length ?? 0) > 0 && (
                <Field label="Tags" hint="Click a tag to exclude its operations">
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {project.tags!.map((tag) => {
                      const excluded = excludedTags.has(tag)
                      return (
                        <button
                          key={tag}
                          type="button"
                          onClick={() => toggleTag(tag)}
                          style={{
                            padding: '4px 10px',
                            borderRadius: 999,
                            fontSize: 12,
                            fontFamily: 'var(--font-mono)',
                            cursor: 'pointer',
                            border: `1px solid ${excluded ? 'var(--border)' : 'var(--text)'}`,
                            background: excluded ? 'var(--surface)' : 'var(--content-bg)',
                            color: excluded ? 'var(--text-faint)' : 'var(--text)',
                            textDecoration: excluded ? 'line-through' : 'none',
                          }}
                        >
                          {tag}
                        </button>
                      )
                    })}
                  </div>
                </Field>
              )}

              <Field label="Deprecated operations">
                <label
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 8,
                    fontSize: 13,
                    color: 'var(--text-dim)',
                    cursor: 'pointer',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={includeDeprecated}
                    onChange={(e) => {
                      setIncludeDeprecated(e.target.checked)
                      setPreview(null)
                    }}
                  />
                  Include operations the spec marks as deprecated
                </label>
              </Field>

              <Field label="Server" hint="Where compiled tools send requests">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {(project.servers ?? []).map((server) => (
                    <label key={server.url} style={radioRowStyle}>
                      <input
                        type="radio"
                        name="server"
                        checked={serverChoice === server.url}
                        onChange={() => pickServer(server.url)}
                      />
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5 }}>
                        {server.url}
                      </span>
                      {server.description && (
                        <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                          {server.description}
                        </span>
                      )}
                    </label>
                  ))}
                  <label style={radioRowStyle}>
                    <input
                      type="radio"
                      name="server"
                      checked={serverChoice === 'custom'}
                      onChange={() => pickServer('custom')}
                    />
                    <span style={{ fontSize: 12.5 }}>Custom base URL</span>
                  </label>
                  {serverChoice === 'custom' && (
                    <input
                      value={customServerUrl}
                      onChange={(e) => {
                        setCustomServerUrl(e.target.value)
                        setPreview(null)
                      }}
                      placeholder="https://api.example.com"
                      spellCheck={false}
                      style={{ ...inputStyle, fontFamily: 'var(--font-mono)', fontSize: 12.5 }}
                    />
                  )}
                  {selectedServer?.variables &&
                    Object.keys(selectedServer.variables).length > 0 && (
                      <div
                        style={{
                          display: 'grid',
                          gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
                          gap: 8,
                          paddingTop: 4,
                        }}
                      >
                        {Object.entries(selectedServer.variables).map(([key, cfg]) => (
                          <label key={key} style={{ display: 'block' }}>
                            <div style={subLabelStyle}>{`{${key}}`}</div>
                            {cfg.enum?.length ? (
                              <select
                                value={serverVariables[key] ?? cfg.default ?? ''}
                                onChange={(e) => {
                                  setServerVariables((v) => ({ ...v, [key]: e.target.value }))
                                  setPreview(null)
                                }}
                                style={{ ...inputStyle, height: 32 }}
                              >
                                {cfg.enum.map((option) => (
                                  <option key={option} value={option}>
                                    {option}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                value={serverVariables[key] ?? ''}
                                onChange={(e) => {
                                  setServerVariables((v) => ({ ...v, [key]: e.target.value }))
                                  setPreview(null)
                                }}
                                style={{ ...inputStyle, height: 32 }}
                              />
                            )}
                          </label>
                        ))}
                      </div>
                    )}
                </div>
              </Field>

              <Field
                label="Authentication"
                hint="Suggested from the spec's security schemes"
                optional
              >
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.2fr)',
                    gap: 8,
                  }}
                >
                  <label style={{ display: 'block' }}>
                    <div style={subLabelStyle}>Header</div>
                    <input
                      value={tokenHeader}
                      onChange={(e) => {
                        setTokenHeader(e.target.value)
                        setPreview(null)
                      }}
                      placeholder="Authorization"
                      style={{ ...inputStyle, fontFamily: 'var(--font-mono)' }}
                    />
                  </label>
                  <label style={{ display: 'block' }}>
                    <div style={subLabelStyle}>Format</div>
                    <input
                      value={tokenFormat}
                      onChange={(e) => {
                        setTokenFormat(e.target.value)
                        setPreview(null)
                      }}
                      placeholder="Bearer {token}"
                      style={{ ...inputStyle, fontFamily: 'var(--font-mono)' }}
                    />
                  </label>
                </div>
                {tokenHeader.trim() && (
                  <div style={{ ...authPreviewStyle, marginTop: 8 }}>
                    <KeyRound size={12} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
                    <span style={{ color: 'var(--syn-tag)' }}>{tokenHeader}</span>
                    <span style={{ color: 'var(--syn-comment)' }}>:</span>
                    <span style={{ color: 'var(--syn-string)' }}>
                      {tokenFormat.replace('{token}', 'sk_•••••') || 'sk_•••••'}
                    </span>
                  </div>
                )}
              </Field>

              <Field label="Integration name">
                <input
                  value={integrationName}
                  onChange={(e) => setIntegrationName(e.target.value)}
                  style={inputStyle}
                />
              </Field>
            </div>

            {compileError && <ErrorBanner message={compileError} />}

            <footer style={cardFooterStyle}>
              <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                Preview shows the exact tools before anything is created.
              </span>
              <Button
                type="button"
                size="sm"
                variant={preview ? 'outline' : 'default'}
                onClick={handlePreview}
                disabled={compiling}
              >
                {compiling ? (
                  <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
                ) : preview ? (
                  'Refresh preview'
                ) : (
                  'Preview tools'
                )}
              </Button>
            </footer>
          </section>
        )}

        {/* ── Step 3: Preview & create ── */}
        {project && preview && (
          <section style={{ ...cardStyle, marginTop: 16 }}>
            <header style={cardHeaderStyle}>
              <h2 style={cardTitleStyle}>
                {preview.tools.length} tool{preview.tools.length === 1 ? '' : 's'} will be created
              </h2>
              <p style={cardSubtitleStyle}>
                Calls go to <code style={{ fontSize: 12 }}>{preview.base_url}</code>. Every tool
                starts in “require approval” mode.
              </p>
            </header>

            <div style={{ maxHeight: 320, overflow: 'auto', padding: '8px 28px' }}>
              {preview.tools.map((tool) => (
                <div
                  key={tool.name}
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 10,
                    padding: '7px 0',
                    borderBottom: '1px solid var(--border)',
                  }}
                >
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 12.5,
                      fontWeight: 600,
                      color: 'var(--text)',
                    }}
                  >
                    {tool.name}
                  </span>
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      color: 'var(--text-dim)',
                    }}
                  >
                    {tool.method} {tool.path}
                  </span>
                  {tool.renamed_from && (
                    <span style={{ fontSize: 10.5, color: 'var(--text-faint)' }}>
                      renamed from {tool.renamed_from}
                    </span>
                  )}
                  <span
                    style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}
                  >
                    {tool.param_count} param{tool.param_count === 1 ? '' : 's'}
                  </span>
                </div>
              ))}
            </div>

            {preview.warnings.length > 0 && (
              <WarningList warnings={preview.warnings} />
            )}

            <footer style={cardFooterStyle}>
              <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                You'll connect credentials on the next screen.
              </span>
              <Button type="button" size="sm" onClick={handleCreate} disabled={creating}>
                {creating ? (
                  <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
                ) : (
                  <span style={{ display: 'inline-flex', alignItems: 'center' }}>
                    Create integration <ArrowRight size={13} style={{ marginLeft: 6 }} />
                  </span>
                )}
              </Button>
            </footer>
          </section>
        )}
      </div>
    </div>
  )
}

function Field({
  label,
  hint,
  optional,
  children,
}: {
  label: string
  hint?: string
  optional?: boolean
  children: React.ReactNode
}) {
  return (
    <div style={{ paddingBottom: 18 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: 'var(--text)',
            textTransform: 'uppercase',
            letterSpacing: 0.5,
          }}
        >
          {label}
        </span>
        {optional && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: 'var(--text-faint)',
              textTransform: 'uppercase',
              letterSpacing: 0.4,
            }}
          >
            optional
          </span>
        )}
        {hint && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--text-faint)',
              marginLeft: 'auto',
              textAlign: 'right',
            }}
          >
            {hint}
          </span>
        )}
      </div>
      {children}
    </div>
  )
}

function SourceCard({
  icon,
  label,
  hint,
  active,
  onClick,
}: {
  icon: React.ReactNode
  label: string
  hint: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      style={{
        textAlign: 'left',
        padding: '11px 12px',
        borderRadius: 8,
        border: `1px solid ${active ? 'var(--text)' : 'var(--border)'}`,
        background: active ? 'var(--content-bg)' : 'var(--surface)',
        cursor: 'pointer',
        fontFamily: 'inherit',
        boxShadow: active ? '0 0 0 1px var(--text) inset' : 'none',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontSize: 12,
          fontWeight: 600,
          color: 'var(--text)',
          marginBottom: 3,
        }}
      >
        {icon}
        {label}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>{hint}</div>
    </button>
  )
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: 'var(--text-faint)',
          textTransform: 'uppercase',
          letterSpacing: 0.4,
          marginBottom: 3,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          color: warn ? 'var(--badge-yellow-text, var(--text))' : 'var(--text)',
        }}
      >
        {value}
      </div>
    </div>
  )
}

function WarningList({ warnings }: { warnings: OpenApiWarning[] }) {
  return (
    <div style={{ padding: '4px 28px 12px' }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: 'var(--text-faint)',
          textTransform: 'uppercase',
          letterSpacing: 0.4,
          margin: '8px 0 6px',
        }}
      >
        Warnings
      </div>
      {warnings.map((w, i) => (
        <div key={i} style={{ fontSize: 12, color: 'var(--text-dim)', padding: '2px 0' }}>
          <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-faint)' }}>
            {w.code}
          </span>{' '}
          {w.message}
          {w.context ? ` (${w.context})` : ''}
        </div>
      ))}
    </div>
  )
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      style={{
        margin: '0 28px 12px',
        padding: '8px 12px',
        borderRadius: 6,
        background: 'var(--badge-red-bg)',
        color: 'var(--badge-red-text)',
        fontSize: 12,
        fontWeight: 500,
      }}
    >
      {message}
    </div>
  )
}

const backLinkStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  border: 'none',
  background: 'transparent',
  color: 'var(--text-dim)',
  fontSize: 12,
  cursor: 'pointer',
  padding: '0 0 16px',
  fontFamily: 'inherit',
}

const cardStyle: React.CSSProperties = {
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  boxShadow: 'var(--card-shadow)',
  overflow: 'hidden',
}

const cardHeaderStyle: React.CSSProperties = {
  padding: '24px 28px 20px',
  borderBottom: '1px solid var(--border)',
}

const cardTitleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 17,
  fontWeight: 600,
  color: 'var(--text)',
  lineHeight: 1.3,
}

const cardSubtitleStyle: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: 13,
  color: 'var(--text-dim)',
  lineHeight: 1.5,
}

const cardFooterStyle: React.CSSProperties = {
  padding: '16px 28px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 12,
  borderTop: '1px solid var(--border)',
  background: 'var(--surface)',
}

const subLabelStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  color: 'var(--text-faint)',
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  marginBottom: 5,
}

const radioRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: 13,
  color: 'var(--text)',
  cursor: 'pointer',
}

const authPreviewStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '8px 12px',
  borderRadius: 6,
  background: 'var(--code-bg)',
  color: 'var(--code-text)',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  overflow: 'hidden',
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  height: 36,
  border: '1px solid var(--border)',
  borderRadius: 7,
  background: 'var(--input-bg)',
  color: 'var(--text)',
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
  padding: '0 11px',
  minWidth: 0,
}

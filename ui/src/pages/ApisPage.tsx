import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AlertTriangle, FileJson, Plus, Trash2 } from 'lucide-react'
import { api, type OpenApiProject } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { formatDateTime, relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSectionLabel,
  SutrTable,
  describeError,
  riskForMethod,
  type Column,
} from '@/components/sutr'

/**
 * Imported OpenAPI specifications — the source material every generated tool
 * comes from. The detail view is the normalised representation the compiler
 * works against, not the raw document.
 */
export default function ApisPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { projects, loaded, load, refresh } = useCatalogStore()

  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<OpenApiProject | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const projectId = params.get('project')
    if (!projectId) return
    let cancelled = false
    api.openapi
      .get(projectId)
      .then((project) => {
        if (!cancelled) setDetail(project)
      })
      .catch((err) => {
        if (!cancelled) setDetailError(describeError(err).message)
      })
    return () => {
      cancelled = true
    }
  }, [params])

  function open(project: OpenApiProject) {
    params.set('project', project.id)
    setParams(params, { replace: true })
  }

  function close() {
    setDetail(null)
    setDetailError(null)
    if (params.get('project')) {
      params.delete('project')
      setParams(params, { replace: true })
    }
  }

  async function remove(project: OpenApiProject) {
    if (
      !window.confirm(
        `Delete the specification “${project.api_title || project.name}”? Tools already compiled from it are not affected.`,
      )
    ) {
      return
    }
    setBusy(true)
    try {
      await api.openapi.remove(project.id)
      close()
      await refresh()
    } catch (err) {
      setDetailError(describeError(err).message)
    } finally {
      setBusy(false)
    }
  }

  const needle = query.trim().toLowerCase()
  const rows = projects.filter(
    (project) =>
      !needle ||
      project.name.toLowerCase().includes(needle) ||
      project.api_title.toLowerCase().includes(needle),
  )

  const columns: Column<OpenApiProject>[] = [
    {
      key: 'title',
      header: 'Specification',
      render: (project) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <span className="sutr-table__primary sutr-truncate">
            {project.api_title || project.name}
          </span>
          <span className="sutr-meta sutr-mono sutr-truncate" style={{ maxWidth: 420 }}>
            {project.source_url ?? project.source_kind}
          </span>
        </div>
      ),
    },
    {
      key: 'version',
      header: 'Version',
      width: 130,
      render: (project) => (
        <span className="sutr-mono">
          {project.api_version || '—'}
          <span className="sutr-meta"> · OpenAPI {project.openapi_version}</span>
        </span>
      ),
    },
    {
      key: 'operations',
      header: 'Operations',
      numeric: true,
      width: 110,
      render: (project) => project.operation_count,
    },
    {
      key: 'warnings',
      header: 'Warnings',
      width: 110,
      render: (project) =>
        project.warnings.length === 0 ? (
          <span className="sutr-meta">none</span>
        ) : (
          <SutrBadge tone="warning" plain>
            {project.warnings.length}
          </SutrBadge>
        ),
    },
    {
      key: 'compiled',
      header: 'Compiled',
      width: 140,
      render: (project) =>
        project.integration_db_id ? (
          <SutrBadge tone="success" dot>
            COMPILED
          </SutrBadge>
        ) : (
          <SutrBadge tone="neutral" dot>
            IMPORTED
          </SutrBadge>
        ),
    },
    {
      key: 'updated',
      header: 'Updated',
      width: 130,
      render: (project) => <span className="sutr-meta">{relativeTime(project.updated_at)}</span>,
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="APIs"
        subtitle="OpenAPI specifications imported into this workspace, normalised and ready to compile into tools."
        actions={
          <SutrButton variant="brand" size="sm" onClick={() => navigate('/app/apis/new')}>
            <Plus size={13} /> Import specification
          </SutrButton>
        }
      />

      <SutrPageBody>
        <div className="sutr-page__toolbar">
          <SutrSearchInput
            value={query}
            onValueChange={setQuery}
            ariaLabel="Search specifications"
            placeholder="Search by title or name"
            maxWidth={340}
          />
          <span className="sutr-meta" style={{ marginLeft: 'auto' }}>
            {rows.length} of {projects.length} specifications
          </span>
        </div>

        {detailError && !detail ? (
          <SutrError what="That specification could not be opened." why={detailError} />
        ) : null}

        {!loaded ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1, 2].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 44 }} />
            ))}
          </div>
        ) : (
          <SutrTable
            columns={columns}
            rows={rows}
            rowKey={(project) => project.id}
            onRowClick={open}
            caption="Imported OpenAPI specifications"
            empty={
              <SutrEmpty
                icon={<FileJson size={17} />}
                title={projects.length === 0 ? 'No specifications imported' : 'No matches'}
                body={
                  projects.length === 0
                    ? 'Import an OpenAPI 3.x document from a file, a URL, GitHub or SwaggerHub. Sutr normalises it and shows you exactly what it found before anything is created.'
                    : 'Nothing matches that search.'
                }
                action={
                  projects.length === 0 ? (
                    <SutrButton variant="brand" size="sm" onClick={() => navigate('/app/apis/new')}>
                      Import a specification
                    </SutrButton>
                  ) : undefined
                }
              />
            }
          />
        )}
      </SutrPageBody>

      <SutrDrawer
        open={Boolean(detail)}
        onClose={close}
        wide
        title={detail?.api_title || detail?.name || ''}
        subtitle={
          detail
            ? `${detail.operation_count} operations · imported ${formatDateTime(detail.created_at)}`
            : ''
        }
        footer={
          detail ? (
            <>
              <SutrButton
                variant="danger"
                size="sm"
                loading={busy}
                onClick={() => void remove(detail)}
              >
                <Trash2 size={13} /> Delete
              </SutrButton>
              <span style={{ flex: 1 }} />
              <SutrButton variant="brand" onClick={() => navigate('/app/apis/new')}>
                Build an MCP server
              </SutrButton>
            </>
          ) : null
        }
      >
        {detail ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {detail.description ? <p className="sutr-body">{detail.description}</p> : null}

            <SutrDefinitionList
              items={[
                { key: 'API version', value: detail.api_version || '—' },
                { key: 'OpenAPI', value: detail.openapi_version },
                { key: 'Source', value: <span className="sutr-mono">{detail.source_kind}</span> },
                {
                  key: 'Source URL',
                  value: detail.source_url ? (
                    <span className="sutr-mono" style={{ overflowWrap: 'anywhere' }}>
                      {detail.source_url}
                    </span>
                  ) : (
                    <span className="sutr-meta">not applicable</span>
                  ),
                },
                { key: 'Status', value: <span className="sutr-mono">{detail.status}</span> },
              ]}
            />

            {detail.servers && detail.servers.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Servers</SutrSectionLabel>
                {detail.servers.map((server) => (
                  <div key={server.url} className="sutr-endpoint">
                    <span className="sutr-endpoint__value">{server.url}</span>
                    {server.description ? (
                      <span className="sutr-meta">{server.description}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            {detail.security_schemes && detail.security_schemes.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Security schemes</SutrSectionLabel>
                {detail.security_schemes.map((scheme, index) => (
                  <div
                    key={`${scheme.name}-${index}`}
                    className="sutr-op-row"
                    style={{ margin: 0 }}
                  >
                    <span className="sutr-mono" style={{ color: 'var(--text)' }}>
                      {scheme.name ?? 'unnamed'}
                    </span>
                    <span className="sutr-meta">
                      {[scheme.type, scheme.scheme, scheme.location, scheme.header_name]
                        .filter(Boolean)
                        .join(' · ')}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}

            {detail.warnings.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Warnings from the import</SutrSectionLabel>
                {detail.warnings.map((warning, index) => (
                  <div
                    key={`${warning.code}-${index}`}
                    style={{
                      display: 'flex',
                      gap: 8,
                      padding: '8px 10px',
                      borderRadius: 'var(--r-sm)',
                      background: 'var(--warning-soft)',
                      border: '1px solid color-mix(in srgb, var(--amber) 26%, transparent)',
                    }}
                  >
                    <AlertTriangle
                      size={13}
                      style={{ color: 'var(--amber)', flexShrink: 0, marginTop: 2 }}
                    />
                    <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                      <span style={{ fontSize: 12.5, color: 'var(--text)' }}>
                        {warning.message}
                      </span>
                      <span className="sutr-meta sutr-mono">
                        {warning.code}
                        {warning.context ? ` · ${warning.context}` : ''}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            ) : null}

            {detail.operations && detail.operations.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Operations</SutrSectionLabel>
                <div style={{ maxHeight: 320, overflowY: 'auto' }}>
                  {detail.operations.map((operation, index) => {
                    const risk = riskForMethod(operation.method)
                    return (
                      <div
                        key={`${operation.method}-${operation.path}-${index}`}
                        className="sutr-op-row"
                        style={{ margin: '0 0 6px' }}
                      >
                        <span className="sutr-op-method" style={{ color: 'var(--text-dim)' }}>
                          {operation.method.toUpperCase()}
                        </span>
                        <span className="sutr-truncate" title={operation.path}>
                          {operation.path}
                        </span>
                        <SutrBadge tone={risk.tone} plain>
                          {risk.label}
                        </SutrBadge>
                      </div>
                    )
                  })}
                </div>
              </div>
            ) : null}

            {detailError ? (
              <SutrError what="That action did not complete." why={detailError} />
            ) : null}
          </div>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}

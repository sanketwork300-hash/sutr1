import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  SutrTabs,
  describeError,
  type Column,
} from '@/components/sutr'
import { formatWhen, v1, type DocumentRecord } from '@/api/v1'
import { useAction, useResource } from './useResource'

type Tab = 'documents' | 'rules' | 'workflows' | 'glossary' | 'search'

const STATUS_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  processed: 'success',
  ready: 'success',
  processing: 'warning',
  queued: 'neutral',
  failed: 'danger',
}

/**
 * Prose turned into things that can be cited (LLD §3.5).
 *
 * Documents, the chunks they were split into, and the rules, workflows and
 * terms extracted from them. Every extracted item keeps its citation — which
 * document, which heading — because a business rule without a source is a
 * claim, not a rule.
 */
export default function DocumentationPage() {
  const [tab, setTab] = useState<Tab>('documents')
  const [selected, setSelected] = useState<DocumentRecord | null>(null)
  const [term, setTerm] = useState('')
  const [results, setResults] = useState<unknown>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const action = useAction()

  const documents = useResource(() => v1.documentation.documents(), [])
  const rules = useResource(() => v1.documentation.rules(), [])
  const workflows = useResource(() => v1.documentation.workflows(), [])
  const glossary = useResource(() => v1.documentation.glossary(), [])
  const capabilities = useResource(() => v1.documentation.capabilities(), [])
  const chunks = useResource<Array<Record<string, unknown>> | null>(
    () => (selected ? v1.documentation.chunks(selected.id) : Promise.resolve(null)),
    [selected?.id],
  )

  async function search() {
    if (!term.trim()) return
    setSearching(true)
    setSearchError(null)
    try {
      setResults(await v1.documentation.search(term.trim()))
    } catch (caught) {
      setSearchError(describeError(caught).message)
    } finally {
      setSearching(false)
    }
  }

  async function reprocess(document: DocumentRecord) {
    const ok = await action.run(
      () => v1.documentation.reprocess(document.id),
      'Reprocessing queued.',
    )
    if (ok) documents.reload()
  }

  const documentColumns: Column<DocumentRecord>[] = [
    {
      key: 'name',
      header: 'Document',
      render: (row) => (
        <>
          <strong>{row.title || row.filename || row.id}</strong>
          <div className="sutr-muted">{row.content_type ?? ''}</div>
        </>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: 130,
      render: (row) => (
        <SutrBadge tone={STATUS_TONE[row.status ?? ''] ?? 'neutral'} dot>
          {row.status ?? 'unknown'}
        </SutrBadge>
      ),
    },
    {
      key: 'bytes',
      header: 'Size',
      numeric: true,
      width: 110,
      render: (row) => (row.bytes ? `${Math.round(row.bytes / 1024)} KB` : '—'),
    },
    { key: 'created', header: 'Uploaded', width: 180, render: (row) => formatWhen(row.created_at) },
    {
      key: 'actions',
      header: '',
      width: 120,
      render: (row) => (
        <SutrButton
          size="sm"
          variant="secondary"
          onClick={(event) => {
            event.stopPropagation()
            reprocess(row)
          }}
          disabled={action.busy}
        >
          Reprocess
        </SutrButton>
      ),
    },
  ]

  function jsonTable(rows: Array<Record<string, unknown>>, empty: string, body: string) {
    if (!rows.length) return <SutrEmpty title={empty} body={body} />
    return (
      <SutrTable
        columns={[
          {
            key: 'row',
            header: 'Extracted',
            render: (row) => (
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {JSON.stringify(row, null, 2)}
              </pre>
            ),
          },
        ]}
        rows={rows}
        rowKey={(row) => String((row as { id?: string }).id ?? JSON.stringify(row).slice(0, 64))}
        minWidth={520}
      />
    )
  }

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Documentation"
        subtitle="Uploaded prose, the chunks it was split into, and the rules, workflows and terms extracted from it — each with its citation."
      >
        <SutrTabs
          items={[
            { value: 'documents', label: 'Documents', count: documents.data?.length },
            { value: 'rules', label: 'Business rules', count: rules.data?.length },
            { value: 'workflows', label: 'Workflows', count: workflows.data?.length },
            { value: 'glossary', label: 'Glossary', count: glossary.data?.length },
            { value: 'search', label: 'Search' },
          ]}
          value={tab}
          onChange={setTab}
          ariaLabel="Documentation view"
        />
      </SutrPageHeader>

      <SutrPageBody>
        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {tab === 'documents' ? (
          <>
            {documents.error ? <SutrError what={documents.error} /> : null}
            {documents.loading ? <SutrSpinner /> : null}
            {documents.data ? (
              documents.data.length ? (
                <SutrTable
                  columns={documentColumns}
                  rows={documents.data}
                  rowKey={(row) => row.id}
                  onRowClick={setSelected}
                  minWidth={780}
                />
              ) : (
                <SutrEmpty
                  title="No documents"
                  body="Upload a specification, a policy or a runbook and this is where its extracted rules and workflows appear."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'rules' ? (
          <>
            {rules.error ? <SutrError what={rules.error} /> : null}
            {rules.data
              ? jsonTable(
                  rules.data,
                  'No business rules extracted',
                  'A rule is extracted with the heading path it came from, so a reader can check it against the source.',
                )
              : null}
          </>
        ) : null}

        {tab === 'workflows' ? (
          <>
            {workflows.error ? <SutrError what={workflows.error} /> : null}
            {workflows.data
              ? jsonTable(
                  workflows.data,
                  'No workflows extracted',
                  'Multi-step procedures found in the prose appear here.',
                )
              : null}
          </>
        ) : null}

        {tab === 'glossary' ? (
          <>
            {glossary.error ? <SutrError what={glossary.error} /> : null}
            {glossary.data
              ? jsonTable(
                  glossary.data,
                  'No glossary terms',
                  'Domain terms and their definitions, as the documents defined them.',
                )
              : null}
          </>
        ) : null}

        {tab === 'search' ? (
          <>
            <div style={{ display: 'flex', gap: 8 }}>
              <SutrSearchInput
                value={term}
                onValueChange={setTerm}
                placeholder="Search the documentation"
                ariaLabel="Documentation search"
                maxWidth={420}
              />
              <SutrButton onClick={search} disabled={searching || !term.trim()}>
                Search
              </SutrButton>
            </div>
            {searchError ? <SutrError what={searchError} /> : null}
            {searching ? <SutrSpinner /> : null}
            {results ? (
              <SutrCard>
                <SutrCardBody>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                    {JSON.stringify(results, null, 2)}
                  </pre>
                </SutrCardBody>
              </SutrCard>
            ) : null}
          </>
        ) : null}

        {capabilities.data ? (
          <>
            <SutrSectionLabel>What this install can parse</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                <pre className="sutr-muted" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                  {JSON.stringify(capabilities.data, null, 2)}
                </pre>
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.title || selected?.filename || ''}
        wide
      >
        <SutrSectionLabel>Chunks</SutrSectionLabel>
        {chunks.loading ? <SutrSpinner /> : null}
        {chunks.error ? <SutrError what={chunks.error} /> : null}
        {chunks.data ? (
          chunks.data.length ? (
            <SutrTable
              columns={[
                {
                  key: 'heading',
                  header: 'Heading path',
                  render: (row) => String(row.heading_path ?? row.heading ?? '—'),
                },
                {
                  key: 'text',
                  header: 'Text',
                  render: (row) => (
                    <span className="sutr-muted">{String(row.text ?? '').slice(0, 240)}</span>
                  ),
                },
              ]}
              rows={chunks.data}
              rowKey={(row) => String(row.id ?? JSON.stringify(row).slice(0, 64))}
              minWidth={640}
            />
          ) : (
            <SutrEmpty title="No chunks" body="This document has not been chunked yet." />
          )
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}

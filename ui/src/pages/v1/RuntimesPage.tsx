import { useState } from 'react'
import {
  SutrBadge,
  SutrCard,
  SutrCardBody,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSegmented,
  SutrSpinner,
  SutrTable,
  type Column,
} from '@/components/sutr'
import { formatWhen, v1, type RuntimeArtifact } from '@/api/v1'
import { useResource } from './useResource'

type Detail = 'manifest' | 'validation' | 'sbom'

/**
 * Generated MCP runtimes (LLD §3.6).
 *
 * An artifact is immutable and identified by the hash of what was built. The
 * three views underneath it are the ones that answer "should I deploy this?":
 * what is in it (manifest), whether it passed (validation), and what it
 * depends on (SBOM).
 */
export default function RuntimesPage() {
  const [selected, setSelected] = useState<RuntimeArtifact | null>(null)
  const [detail, setDetail] = useState<Detail>('manifest')

  const artifacts = useResource(() => v1.generation.runtimes(), [])
  const capabilities = useResource(() => v1.generation.capabilities(), [])
  const document = useResource<Record<string, unknown> | null>(() => {
    if (!selected) return Promise.resolve(null)
    if (detail === 'manifest') return v1.generation.manifest(selected.id)
    if (detail === 'validation') return v1.generation.validation(selected.id)
    return v1.generation.sbom(selected.id)
  }, [selected?.id, detail])

  const columns: Column<RuntimeArtifact>[] = [
    {
      key: 'name',
      header: 'Artifact',
      render: (row) => (
        <>
          <strong>{row.name || row.id}</strong>
          {row.build_hash ? <div className="sutr-muted">{row.build_hash.slice(0, 16)}…</div> : null}
        </>
      ),
    },
    {
      key: 'state',
      header: 'State',
      width: 130,
      render: (row) => (
        <SutrBadge
          tone={row.state === 'validated' || row.state === 'ready' ? 'success' : 'neutral'}
          dot
        >
          {row.state ?? 'unknown'}
        </SutrBadge>
      ),
    },
    {
      key: 'signed',
      header: 'Signed',
      width: 130,
      // An unsigned artifact says so plainly: signing needs a key this install
      // may not have, and a blank column would read as "signed".
      render: (row) =>
        row.signed ? (
          <SutrBadge tone="success">signed</SutrBadge>
        ) : (
          <span className="sutr-muted">unsigned</span>
        ),
    },
    { key: 'created', header: 'Built', width: 180, render: (row) => formatWhen(row.created_at) },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Runtimes"
        subtitle="Generated MCP server artifacts: what is in each one, whether it passed validation, and what it depends on."
      />

      <SutrPageBody>
        {artifacts.error ? <SutrError what={artifacts.error} /> : null}
        {artifacts.loading ? <SutrSpinner /> : null}
        {artifacts.data ? (
          artifacts.data.length ? (
            <SutrTable
              columns={columns}
              rows={artifacts.data}
              rowKey={(row) => row.id}
              onRowClick={(row) => {
                setSelected(row)
                setDetail('manifest')
              }}
              minWidth={720}
            />
          ) : (
            <SutrEmpty
              title="No runtimes generated"
              body="Compiling an API into an MCP server produces an artifact here, identified by the hash of what was built."
            />
          )
        ) : null}

        {capabilities.data ? (
          <>
            <SutrSectionLabel>What generation can do here</SutrSectionLabel>
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
        title={selected?.name || selected?.id || ''}
        subtitle={selected?.build_hash}
        wide
        actions={
          <SutrSegmented
            items={[
              { value: 'manifest', label: 'Manifest' },
              { value: 'validation', label: 'Validation' },
              { value: 'sbom', label: 'SBOM' },
            ]}
            value={detail}
            onChange={setDetail}
            ariaLabel="Artifact detail"
          />
        }
      >
        {selected ? (
          <SutrDefinitionList
            items={[
              { key: 'State', value: selected.state ?? '—' },
              { key: 'Signed', value: selected.signed ? 'yes' : 'no' },
              { key: 'Built', value: formatWhen(selected.created_at) },
            ]}
          />
        ) : null}

        {document.loading ? <SutrSpinner /> : null}
        {document.error ? <SutrError what={document.error} /> : null}
        {document.data ? (
          <SutrCard>
            <SutrCardBody>
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {JSON.stringify(document.data, null, 2)}
              </pre>
            </SutrCardBody>
          </SutrCard>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}

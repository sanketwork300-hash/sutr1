import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronRight,
  Download,
  FileJson,
  Code2,
  Globe,
  KeyRound,
  Loader2,
  Lock,
  Rocket,
  Search,
  Upload,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import '@/components/mcp-builder/builder.css'
import { BuilderRail, BuilderSummary } from '@/components/mcp-builder/BuilderRail'
import { SutrButton, SutrPageHeader } from '@/components/sutr'
import {
  api,
  type OpenApiCompileResult,
  type OpenApiDiscoverResult,
  type OpenApiProject,
  type ConnectionProviderInfo,
  type OpenApiSourceKind,
} from '@/api/client'
import {
  Card,
  ChoiceButton,
  Field,
  Footer,
  NextLabel,
  SecretInput,
  Stat,
  WarningList,
} from '@/components/mcp-builder/primitives'
import {
  authPreviewStyle,
  bannerStyle,
  inputStyle,
  linkButtonStyle,
  monoInputStyle,
  radioRowStyle,
  spin,
  subLabelStyle,
} from '@/components/mcp-builder/styles'
import { DeployStep } from '@/components/mcp-builder/DeployStep'
import { GithubSource, type GithubAuthMode } from '@/components/mcp-builder/GithubSource'
import { UploadSource } from '@/components/mcp-builder/UploadSource'
import { useProviderConnections } from '@/components/connections/useProviderConnections'

/**
 * The MCP builder: one visible pass of the compiler pipeline.
 *
 *   Source -> (pick file) -> Normalize + IR -> Select operations -> Auth -> Build -> Deploy
 *
 * Each stage names what the pipeline actually did, so a failure is attributable
 * to a stage rather than to "the import".
 */

type StepId = 'source' | 'file' | 'review' | 'select' | 'auth' | 'build' | 'deploy'

const STEP_LABELS: Record<StepId, string> = {
  source: 'Source',
  file: 'Choose file',
  review: 'Normalize & IR',
  select: 'Select tools',
  auth: 'Authentication',
  build: 'Build',
  deploy: 'Deploy',
}

const SOURCES: {
  kind: OpenApiSourceKind
  label: string
  icon: React.ReactNode
  hint: string
}[] = [
  { kind: 'github', label: 'GitHub', icon: <Code2 size={14} />, hint: 'Connect, or paste a URL' },
  {
    kind: 'swaggerhub',
    label: 'SwaggerHub',
    icon: <Globe size={14} />,
    hint: 'API page URL',
  },
  { kind: 'upload', label: 'Upload', icon: <Upload size={14} />, hint: 'A file from this machine' },
  { kind: 'url', label: 'Direct URL', icon: <Globe size={14} />, hint: 'Public spec URL' },
  { kind: 'paste', label: 'Paste', icon: <FileJson size={14} />, hint: 'JSON or YAML' },
]

export default function McpBuilderPage() {
  const navigate = useNavigate()
  const [step, setStep] = useState<StepId>('source')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // Stage 1 — source
  const [sourceKind, setSourceKind] = useState<OpenApiSourceKind>('github')
  const [url, setUrl] = useState('')
  const [content, setContent] = useState('')
  const [githubToken, setGithubToken] = useState('')
  const [githubAuth, setGithubAuth] = useState<GithubAuthMode>('connection')
  const [swaggerhubKey, setSwaggerhubKey] = useState('')
  const [uploadFilename, setUploadFilename] = useState('')
  const [name, setName] = useState('')

  // Connected accounts are read once here and handed down: the source stage
  // and the deploy stage both need them, and two independent fetches would
  // let them disagree about whether GitHub is connected.
  const connections = useProviderConnections()

  // Stage 2 — file choice (GitHub repos with several specs)
  const [discovery, setDiscovery] = useState<OpenApiDiscoverResult | null>(null)
  const [chosenPath, setChosenPath] = useState('')

  // Stage 3 — the imported project (normalized + IR stored server-side)
  const [project, setProject] = useState<OpenApiProject | null>(null)

  // Stage 4 — operation selection
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [includeDeprecated, setIncludeDeprecated] = useState(false)

  // Stage 5 — auth
  const [tokenHeader, setTokenHeader] = useState('')
  const [tokenFormat, setTokenFormat] = useState('')

  // Stage 6 — build
  const [serverChoice, setServerChoice] = useState('')
  const [customServerUrl, setCustomServerUrl] = useState('')
  const [serverVariables, setServerVariables] = useState<Record<string, string>>({})
  const [integrationName, setIntegrationName] = useState('')
  const [preview, setPreview] = useState<OpenApiCompileResult | null>(null)
  const [created, setCreated] = useState<OpenApiCompileResult | null>(null)

  const steps = useMemo<StepId[]>(() => {
    const base: StepId[] = ['source']
    if (discovery && discovery.candidates.length > 1) base.push('file')
    return [...base, 'review', 'select', 'auth', 'build', 'deploy']
  }, [discovery])

  // Whether GitHub requests carry the stored connection or a pasted token.
  // Resolved in one place so discover and import can never disagree.
  const useGithubConnection = sourceKind === 'github' && githubAuth === 'connection'

  const stepIndex = steps.indexOf(step)
  const operations = useMemo(() => project?.operations ?? [], [project])
  const selectableIds = useMemo(
    () => operations.filter((op) => op.operation_id).map((op) => op.operation_id as string),
    [operations],
  )
  const unfilterable = operations.filter((op) => !op.operation_id)

  const selectedServer = useMemo(() => {
    if (!project?.servers || serverChoice === 'custom') return null
    return project.servers.find((server) => server.url === serverChoice) ?? null
  }, [project, serverChoice])

  function fail(e: unknown, fallback: string) {
    setError(e instanceof Error ? e.message : fallback)
  }

  // ── Stage 1/2: discover then import ───────────────────────────────────────

  async function handleDiscover() {
    setError('')
    if (!url.trim()) {
      setError('Paste a GitHub repository or file URL first.')
      return
    }
    setBusy(true)
    try {
      const result = await api.openapi.discover({
        url: url.trim(),
        use_connection: useGithubConnection,
        github_token: useGithubConnection ? undefined : githubToken.trim() || undefined,
      })
      setDiscovery(result)
      if (result.candidates.length === 0) {
        setError(
          `No OpenAPI or Swagger file found in ${result.owner}/${result.repo} (branch ${result.branch}). ` +
            'Link the file directly instead.',
        )
        return
      }
      setChosenPath(result.candidates[0]!.path)
      if (result.candidates.length === 1) {
        await runImport(result.candidates[0]!.path)
      } else {
        setStep('file')
      }
    } catch (e) {
      fail(e, 'Could not inspect that repository')
    } finally {
      setBusy(false)
    }
  }

  const runImport = useCallback(
    async (path?: string) => {
      setError('')
      setBusy(true)
      try {
        const inline = sourceKind === 'paste' || sourceKind === 'upload'
        const imported = await api.openapi.import({
          name: name.trim() || undefined,
          source_kind: sourceKind,
          url: inline ? undefined : url.trim(),
          content: inline ? content : undefined,
          filename: sourceKind === 'upload' ? uploadFilename || undefined : undefined,
          path: path || undefined,
          use_connection: useGithubConnection,
          github_token:
            sourceKind === 'github' && !useGithubConnection
              ? githubToken.trim() || undefined
              : undefined,
          swaggerhub_api_key:
            sourceKind === 'swaggerhub' ? swaggerhubKey.trim() || undefined : undefined,
        })
        setProject(imported)
        setIntegrationName(imported.name)
        setTokenHeader(imported.suggested_auth?.token_header ?? '')
        setTokenFormat(imported.suggested_auth?.token_format ?? '')
        const firstServer = imported.servers?.[0]?.url
        setServerChoice(firstServer ?? 'custom')
        const vars: Record<string, string> = {}
        for (const [key, cfg] of Object.entries(imported.servers?.[0]?.variables ?? {})) {
          vars[key] = cfg.default ?? ''
        }
        setServerVariables(vars)
        // Everything selected by default: narrowing is a deliberate act.
        setSelected(
          new Set(
            (imported.operations ?? [])
              .filter((op) => op.operation_id && (includeDeprecated || !op.deprecated))
              .map((op) => op.operation_id as string),
          ),
        )
        setStep('review')
      } catch (e) {
        fail(e, 'Import failed')
      } finally {
        setBusy(false)
      }
    },
    [
      content,
      githubToken,
      includeDeprecated,
      name,
      sourceKind,
      swaggerhubKey,
      uploadFilename,
      url,
      useGithubConnection,
    ],
  )

  // ── Stage 6: compile ──────────────────────────────────────────────────────

  const compileBody = useCallback(
    (dryRun: boolean) => {
      const allSelected = selected.size === selectableIds.length
      return {
        dry_run: dryRun,
        filters: {
          // Only send an operation filter when the user actually narrowed the
          // set — an empty include list means "everything" server-side.
          include_operations: allSelected ? [] : [...selected],
          include_deprecated: includeDeprecated,
        },
        server_url: serverChoice === 'custom' ? customServerUrl.trim() : serverChoice,
        server_variables: serverVariables,
        auth: { token_header: tokenHeader.trim(), token_format: tokenFormat },
        integration_name: integrationName.trim() || undefined,
      }
    },
    [
      customServerUrl,
      includeDeprecated,
      integrationName,
      selectableIds.length,
      selected,
      serverChoice,
      serverVariables,
      tokenFormat,
      tokenHeader,
    ],
  )

  async function handlePreview() {
    if (!project) return
    setError('')
    if (serverChoice === 'custom' && !customServerUrl.trim()) {
      setError('Enter the base URL that tool calls should go to.')
      return
    }
    setBusy(true)
    try {
      setPreview(await api.openapi.compile(project.id, compileBody(true)))
    } catch (e) {
      fail(e, 'Compilation failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleCreate() {
    if (!project) return
    setError('')
    setBusy(true)
    try {
      setCreated(await api.openapi.compile(project.id, compileBody(false)))
    } catch (e) {
      fail(e, 'Could not create the integration')
    } finally {
      setBusy(false)
    }
  }

  async function handleDownload() {
    if (!project) return
    setError('')
    setBusy(true)
    try {
      const { blob, filename } = await api.openapi.downloadPackage(project.id, compileBody(false))
      const href = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = href
      anchor.download = filename ?? 'mcp-server.zip'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(href)
    } catch (e) {
      fail(e, 'Could not generate the package')
    } finally {
      setBusy(false)
    }
  }

  // Re-preview whenever the selection changes underneath an existing preview.
  useEffect(() => {
    setPreview(null)
    setCreated(null)
  }, [selected, includeDeprecated, tokenHeader, tokenFormat, serverChoice, customServerUrl])

  const selectedToolCount = selected.size || selectableIds.length

  return (
    <div className="sutr-page">
      <SutrPageHeader
        eyebrow="Build"
        title="MCP builder"
        subtitle="Turn an OpenAPI specification into governed agent tools, one stage at a time."
        actions={
          <SutrButton variant="ghost" size="sm" onClick={() => navigate('/app/apis')}>
            <ArrowLeft size={13} /> APIs
          </SutrButton>
        }
      />

      <div className="mb">
        <div className="mb__main">
          <BuilderRail
            stages={steps.map((id) => ({ id, label: STEP_LABELS[id] }))}
            current={step}
            onJump={(target) => setStep(target)}
          />

          {error && (
            <div style={bannerStyle('error')}>
              <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>{error}</span>
            </div>
          )}

        {step === 'source' && (
          <SourceStep
            sourceKind={sourceKind}
            onSourceKind={(kind) => {
              setSourceKind(kind)
              setDiscovery(null)
              setError('')
            }}
            url={url}
            onUrl={setUrl}
            content={content}
            onContent={setContent}
            githubToken={githubToken}
            onGithubToken={setGithubToken}
            githubAuth={githubAuth}
            onGithubAuth={setGithubAuth}
            githubProvider={connections.find('github')}
            onConnectionsChanged={connections.reload}
            uploadFilename={uploadFilename}
            onUploadFilename={setUploadFilename}
            swaggerhubKey={swaggerhubKey}
            onSwaggerhubKey={setSwaggerhubKey}
            name={name}
            onName={setName}
            busy={busy}
            onNext={() => (sourceKind === 'github' ? handleDiscover() : runImport())}
          />
        )}

        {step === 'file' && discovery && (
          <FileStep
            discovery={discovery}
            chosenPath={chosenPath}
            onChoose={setChosenPath}
            busy={busy}
            onBack={() => setStep('source')}
            onNext={() => runImport(chosenPath)}
          />
        )}

        {step === 'review' && project && (
          <ReviewStep
            project={project}
            onBack={() => setStep(steps.includes('file') ? 'file' : 'source')}
            onNext={() => setStep('select')}
          />
        )}

        {step === 'select' && project && (
          <SelectStep
            project={project}
            selected={selected}
            onToggle={(id) =>
              setSelected((prev) => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })
            }
            onSelectAll={() => setSelected(new Set(selectableIds))}
            onSelectNone={() => setSelected(new Set())}
            includeDeprecated={includeDeprecated}
            onIncludeDeprecated={setIncludeDeprecated}
            unfilterableCount={unfilterable.length}
            onBack={() => setStep('review')}
            onNext={() => setStep('auth')}
          />
        )}

        {step === 'auth' && project && (
          <AuthStep
            project={project}
            tokenHeader={tokenHeader}
            onTokenHeader={setTokenHeader}
            tokenFormat={tokenFormat}
            onTokenFormat={setTokenFormat}
            onBack={() => setStep('select')}
            onNext={() => setStep('build')}
          />
        )}

        {step === 'build' && project && (
          <BuildStep
            project={project}
            selectedServer={selectedServer}
            serverChoice={serverChoice}
            onServerChoice={(choice) => {
              setServerChoice(choice)
              const server = project.servers?.find((s) => s.url === choice)
              const vars: Record<string, string> = {}
              for (const [key, cfg] of Object.entries(server?.variables ?? {})) {
                vars[key] = cfg.default ?? ''
              }
              setServerVariables(vars)
            }}
            customServerUrl={customServerUrl}
            onCustomServerUrl={setCustomServerUrl}
            serverVariables={serverVariables}
            onServerVariable={(key, value) =>
              setServerVariables((prev) => ({ ...prev, [key]: value }))
            }
            integrationName={integrationName}
            onIntegrationName={setIntegrationName}
            selectedCount={selected.size || selectableIds.length}
            preview={preview}
            created={created}
            busy={busy}
            onPreview={handlePreview}
            onCreate={handleCreate}
            onDownload={handleDownload}
            onBack={() => setStep('auth')}
            onOpenIntegration={() =>
              navigate(`/app/integrations/custom-api/${created?.integration_db_id}`)
            }
            onDeploy={() => setStep('deploy')}
          />
        )}

        {step === 'deploy' && project && (
          <DeployStep
            project={project}
            compileBody={compileBody}
            suggestedName={integrationName || project.name}
            onBack={() => setStep('build')}
            onOpenDeployments={() => navigate('/app/deployments')}
          />
        )}

          <p style={{ fontSize: 11, color: 'var(--text-faint)' }}>
            Stage {stepIndex + 1} of {steps.length} · Nothing is created until the build stage.
          </p>
        </div>

        <aside className="mb__aside">
          <BuilderSummary
            error={error || null}
            warnings={project?.warnings.length ?? 0}
            rows={[
              { key: 'Source', value: sourceKind },
              {
                key: 'Specification',
                value: project ? project.api_title || project.name : 'not imported',
              },
              { key: 'Version', value: project?.api_version || '—' },
              {
                key: 'Operations',
                value: project ? String(project.operation_count) : '—',
              },
              {
                key: 'Selected',
                value: project ? `${selectedToolCount} tool${selectedToolCount === 1 ? '' : 's'}` : '—',
              },
              {
                key: 'Auth',
                value: tokenHeader ? tokenHeader : (project?.security_schemes?.length ?? 0) > 0 ? 'from spec' : 'none',
              },
              {
                key: 'Server',
                value: serverChoice || customServerUrl || project?.server_url || '—',
              },
            ]}
            footer={
              created?.integration_id ? (
                <span className="sutr-meta" style={{ color: 'var(--green)' }}>
                  Integration created: {created.integration_id}
                </span>
              ) : null
            }
          />
        </aside>
      </div>
    </div>
  )
}

// ── Stage 1: source ─────────────────────────────────────────────────────────

function SourceStep({
  sourceKind,
  onSourceKind,
  url,
  onUrl,
  content,
  onContent,
  githubToken,
  onGithubToken,
  githubAuth,
  onGithubAuth,
  githubProvider,
  onConnectionsChanged,
  uploadFilename,
  onUploadFilename,
  swaggerhubKey,
  onSwaggerhubKey,
  name,
  onName,
  busy,
  onNext,
}: {
  sourceKind: OpenApiSourceKind
  onSourceKind: (kind: OpenApiSourceKind) => void
  url: string
  onUrl: (value: string) => void
  content: string
  onContent: (value: string) => void
  githubToken: string
  onGithubToken: (value: string) => void
  githubAuth: GithubAuthMode
  onGithubAuth: (mode: GithubAuthMode) => void
  githubProvider: ConnectionProviderInfo | undefined
  onConnectionsChanged: () => void
  uploadFilename: string
  onUploadFilename: (value: string) => void
  swaggerhubKey: string
  onSwaggerhubKey: (value: string) => void
  name: string
  onName: (value: string) => void
  busy: boolean
  onNext: () => void
}) {
  return (
    <Card
      title="Where is the specification?"
      subtitle="Sutr fetches it server-side, validates it against the OpenAPI schemas, and refuses external $refs."
    >
      <div style={{ padding: '18px 24px 4px' }}>
        <Field label="Source">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
            {SOURCES.map((source) => (
              <ChoiceButton
                key={source.kind}
                active={sourceKind === source.kind}
                onClick={() => onSourceKind(source.kind)}
                icon={source.icon}
                label={source.label}
                hint={source.hint}
              />
            ))}
          </div>
        </Field>

        {sourceKind === 'github' && (
          <GithubSource
            provider={githubProvider}
            onProviderChanged={onConnectionsChanged}
            mode={githubAuth}
            onMode={onGithubAuth}
            url={url}
            onUrl={onUrl}
            token={githubToken}
            onToken={onGithubToken}
          />
        )}

        {sourceKind === 'swaggerhub' && (
          <>
            <Field
              label="SwaggerHub API URL"
              help="Open the API in SwaggerHub and copy the address bar. Sutr reads the owner, API name, and version from it and pulls the definition from the SwaggerHub registry with external $refs already inlined."
              examples={['https://app.swaggerhub.com/apis/owner/api-name/1.0.0']}
            >
              <input
                value={url}
                onChange={(event) => onUrl(event.target.value)}
                placeholder="https://app.swaggerhub.com/apis/owner/api-name/1.0.0"
                spellCheck={false}
                style={monoInputStyle}
              />
            </Field>
            <Field
              label="SwaggerHub API key"
              optional
              help="Only needed for a private API. Find it in SwaggerHub under your avatar → API Key. SwaggerHub expects the key as-is (not a Bearer token) — paste it exactly. Used for this import only and never stored."
            >
              <SecretInput
                value={swaggerhubKey}
                onChange={onSwaggerhubKey}
                placeholder="Paste the key exactly as SwaggerHub shows it"
              />
            </Field>
          </>
        )}

        {sourceKind === 'upload' && (
          <UploadSource
            content={content}
            onContent={onContent}
            filename={uploadFilename}
            onFilename={onUploadFilename}
          />
        )}

        {sourceKind === 'url' && (
          <Field
            label="Specification URL"
            help="Must be reachable from the Sutr server and publicly readable. Private hosts (localhost, 10.x, 192.168.x, link-local) are refused, and redirects are not followed — use the final URL."
            examples={['https://petstore3.swagger.io/api/v3/openapi.json']}
          >
            <input
              value={url}
              onChange={(event) => onUrl(event.target.value)}
              placeholder="https://api.example.com/openapi.json"
              spellCheck={false}
              style={monoInputStyle}
            />
          </Field>
        )}

        {sourceKind === 'paste' && (
          <Field
            label="Specification"
            help="Paste the whole document - JSON or YAML, OpenAPI 3.0 or 3.1. Swagger 2.0 is rejected; convert it first. To use a file from this machine, choose Upload instead."
          >
            <textarea
              value={content}
              onChange={(event) => onContent(event.target.value)}
              placeholder={'{\n  "openapi": "3.0.3",\n  "info": { "title": "...", "version": "1.0.0" },\n  "paths": { ... }\n}'}
              spellCheck={false}
              style={{ ...monoInputStyle, height: 190, padding: '10px 11px', resize: 'vertical' }}
            />
          </Field>
        )}

        <Field label="Project name" optional help="Defaults to the API title from the spec.">
          <input
            value={name}
            onChange={(event) => onName(event.target.value)}
            placeholder="e.g. Petstore"
            style={inputStyle}
          />
        </Field>
      </div>

      <Footer
        hint={
          sourceKind === 'github'
            ? 'Next: sutr looks for specification files in the repository.'
            : sourceKind === 'upload'
              ? 'Next: sutr parses, validates, and normalizes the file.'
              : 'Next: sutr fetches and normalizes the specification.'
        }
      >
        <Button
          size="sm"
          onClick={onNext}
          disabled={
            busy ||
            ((sourceKind === 'upload' || sourceKind === 'paste') && !content.trim()) ||
            (sourceKind !== 'upload' && sourceKind !== 'paste' && !url.trim())
          }
        >
          {busy ? (
            <Loader2 size={13} style={spin} />
          ) : (
            <NextLabel>{sourceKind === 'github' ? 'Find specs' : 'Import'}</NextLabel>
          )}
        </Button>
      </Footer>
    </Card>
  )
}

// ── Stage 2: file choice ────────────────────────────────────────────────────

function FileStep({
  discovery,
  chosenPath,
  onChoose,
  busy,
  onBack,
  onNext,
}: {
  discovery: OpenApiDiscoverResult
  chosenPath: string
  onChoose: (path: string) => void
  busy: boolean
  onBack: () => void
  onNext: () => void
}) {
  return (
    <Card
      title={`${discovery.candidates.length} specifications found`}
      subtitle={`${discovery.owner}/${discovery.repo} on branch ${discovery.branch}. The first is Sutr's best guess — conventional names and shallower paths rank higher.`}
    >
      <div style={{ maxHeight: 340, overflow: 'auto', padding: '8px 24px' }}>
        {discovery.candidates.map((candidate) => (
          <label
            key={candidate.path}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '9px 0',
              borderBottom: '1px solid var(--border)',
              cursor: 'pointer',
            }}
          >
            <input
              type="radio"
              name="spec-file"
              checked={chosenPath === candidate.path}
              onChange={() => onChoose(candidate.path)}
            />
            <span
              style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5, color: 'var(--text)' }}
            >
              {candidate.path}
            </span>
            {candidate.size != null && (
              <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                {(candidate.size / 1024).toFixed(1)} KB
              </span>
            )}
          </label>
        ))}
      </div>
      <Footer hint="Next: parse, validate, and normalize the chosen file." onBack={onBack}>
        <Button size="sm" onClick={onNext} disabled={busy || !chosenPath}>
          {busy ? <Loader2 size={13} style={spin} /> : <NextLabel>Normalize</NextLabel>}
        </Button>
      </Footer>
    </Card>
  )
}

// ── Stage 3: normalize + IR ─────────────────────────────────────────────────

function ReviewStep({
  project,
  onBack,
  onNext,
}: {
  project: OpenApiProject
  onBack: () => void
  onNext: () => void
}) {
  const [showIr, setShowIr] = useState(false)
  return (
    <Card
      title="Normalized"
      subtitle="The document validated against the OpenAPI schemas. $refs were resolved with cycles bounded, parameters and request bodies flattened into a single intermediate representation."
    >
      <div style={{ padding: '16px 24px', display: 'flex', gap: 20, flexWrap: 'wrap' }}>
        <Stat label="API" value={project.api_title} />
        <Stat label="Version" value={project.api_version} />
        <Stat label="OpenAPI" value={project.openapi_version} />
        <Stat label="Operations" value={String(project.operation_count)} />
        <Stat label="Servers" value={String(project.servers?.length ?? 0)} />
        <Stat
          label="Security schemes"
          value={String(project.security_schemes?.length ?? 0)}
        />
      </div>

      {project.warnings.length > 0 && <WarningList warnings={project.warnings} />}

      <div style={{ padding: '0 24px 16px' }}>
        <button type="button" onClick={() => setShowIr((value) => !value)} style={linkButtonStyle}>
          <ChevronRight
            size={13}
            style={{ transform: showIr ? 'rotate(90deg)' : 'none', transition: 'transform 120ms' }}
          />
          {showIr ? 'Hide' : 'Show'} intermediate representation ({project.operation_count}{' '}
          operations)
        </button>
        {showIr && (
          <div
            style={{
              marginTop: 8,
              maxHeight: 260,
              overflow: 'auto',
              border: '1px solid var(--border)',
              borderRadius: 8,
            }}
          >
            {(project.operations ?? []).map((op) => (
              <div
                key={`${op.method}-${op.path}`}
                style={{
                  display: 'flex',
                  gap: 10,
                  padding: '6px 10px',
                  borderBottom: '1px solid var(--border)',
                  fontSize: 11.5,
                }}
              >
                <span
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontWeight: 600,
                    color: 'var(--syn-tag)',
                    minWidth: 46,
                  }}
                >
                  {op.method}
                </span>
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text)' }}>
                  {op.path}
                </span>
                <span style={{ color: 'var(--text-faint)', marginLeft: 'auto' }}>
                  {op.operation_id ?? 'no operationId'}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <Footer hint="Next: choose which operations become tools." onBack={onBack}>
        <Button size="sm" onClick={onNext}>
          <NextLabel>Select tools</NextLabel>
        </Button>
      </Footer>
    </Card>
  )
}

// ── Stage 4: operation selection ────────────────────────────────────────────

function SelectStep({
  project,
  selected,
  onToggle,
  onSelectAll,
  onSelectNone,
  includeDeprecated,
  onIncludeDeprecated,
  unfilterableCount,
  onBack,
  onNext,
}: {
  project: OpenApiProject
  selected: Set<string>
  onToggle: (id: string) => void
  onSelectAll: () => void
  onSelectNone: () => void
  includeDeprecated: boolean
  onIncludeDeprecated: (value: boolean) => void
  unfilterableCount: number
  onBack: () => void
  onNext: () => void
}) {
  const [query, setQuery] = useState('')
  const [tag, setTag] = useState('')
  const operations = project.operations ?? []
  const tags = project.tags ?? []

  const visible = operations.filter((op) => {
    if (!includeDeprecated && op.deprecated) return false
    if (tag && !op.tags.includes(tag)) return false
    if (!query.trim()) return true
    const needle = query.toLowerCase()
    return (
      op.path.toLowerCase().includes(needle) ||
      (op.operation_id ?? '').toLowerCase().includes(needle) ||
      (op.summary ?? '').toLowerCase().includes(needle)
    )
  })

  return (
    <Card
      title="Select tools"
      subtitle="Each operation becomes one agent tool. Import only what the agent needs — a smaller, sharper tool list makes the model choose better."
    >
      <div style={{ padding: '14px 24px 6px', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <div style={{ position: 'relative', flex: 1, minWidth: 200 }}>
          <Search
            size={13}
            style={{ position: 'absolute', left: 10, top: 11, color: 'var(--text-faint)' }}
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by path, operationId, or summary"
            style={{ ...inputStyle, paddingLeft: 30 }}
          />
        </div>
        {tags.length > 0 && (
          <select value={tag} onChange={(event) => setTag(event.target.value)} style={{ ...inputStyle, width: 170 }}>
            <option value="">All tags</option>
            {tags.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        )}
      </div>

      <div
        style={{
          padding: '0 24px 8px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          fontSize: 11.5,
          color: 'var(--text-dim)',
        }}
      >
        <button type="button" onClick={onSelectAll} style={linkButtonStyle}>
          Select all
        </button>
        <button type="button" onClick={onSelectNone} style={linkButtonStyle}>
          Clear
        </button>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={includeDeprecated}
            onChange={(event) => onIncludeDeprecated(event.target.checked)}
          />
          Include deprecated
        </label>
        <span style={{ marginLeft: 'auto', fontWeight: 600, color: 'var(--text)' }}>
          {selected.size} selected
        </span>
      </div>

      <div style={{ maxHeight: 330, overflow: 'auto', padding: '0 24px' }}>
        {visible.length === 0 && (
          <p style={{ fontSize: 12.5, color: 'var(--text-faint)', padding: '16px 0' }}>
            No operations match that filter.
          </p>
        )}
        {visible.map((op) => {
          const id = op.operation_id
          const isSelectable = Boolean(id)
          return (
            <label
              key={`${op.method}-${op.path}`}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 9,
                padding: '7px 0',
                borderBottom: '1px solid var(--border)',
                cursor: isSelectable ? 'pointer' : 'default',
                opacity: isSelectable ? 1 : 0.7,
              }}
            >
              {isSelectable ? (
                <input
                  type="checkbox"
                  checked={selected.has(id as string)}
                  onChange={() => onToggle(id as string)}
                  style={{ marginTop: 3 }}
                />
              ) : (
                <span title="This operation has no operationId, so it cannot be selected individually. It is included; exclude it by tag if needed.">
                  <Lock
                    size={12}
                    style={{ marginTop: 4, color: 'var(--text-faint)', flexShrink: 0 }}
                  />
                </span>
              )}
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      fontWeight: 600,
                      color: 'var(--syn-tag)',
                    }}
                  >
                    {op.method}
                  </span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text)' }}>
                    {op.path}
                  </span>
                  {op.deprecated && (
                    <span style={{ fontSize: 10, color: 'var(--badge-amber-text)' }}>
                      DEPRECATED
                    </span>
                  )}
                </div>
                {op.summary && (
                  <div style={{ fontSize: 11.5, color: 'var(--text-dim)', marginTop: 1 }}>
                    {op.summary}
                  </div>
                )}
              </div>
            </label>
          )
        })}
      </div>

      {unfilterableCount > 0 && (
        <p style={{ padding: '10px 24px 0', fontSize: 11, color: 'var(--text-faint)' }}>
          {unfilterableCount} operation{unfilterableCount === 1 ? '' : 's'} have no{' '}
          <code>operationId</code>, so they cannot be picked individually — they are included, and
          can only be excluded by tag.
        </p>
      )}

      <Footer hint="Next: how tool calls authenticate to the API." onBack={onBack}>
        <Button size="sm" onClick={onNext} disabled={selected.size === 0 && unfilterableCount === 0}>
          <NextLabel>Authentication</NextLabel>
        </Button>
      </Footer>
    </Card>
  )
}

// ── Stage 5: auth ───────────────────────────────────────────────────────────

function AuthStep({
  project,
  tokenHeader,
  onTokenHeader,
  tokenFormat,
  onTokenFormat,
  onBack,
  onNext,
}: {
  project: OpenApiProject
  tokenHeader: string
  onTokenHeader: (value: string) => void
  tokenFormat: string
  onTokenFormat: (value: string) => void
  onBack: () => void
  onNext: () => void
}) {
  const schemes = project.security_schemes ?? []
  const presets = [
    { label: 'Bearer token', header: 'Authorization', format: 'Bearer {token}' },
    { label: 'API key header', header: 'X-API-Key', format: '{token}' },
    { label: 'Basic', header: 'Authorization', format: 'Basic {token}' },
    { label: 'No auth', header: '', format: '' },
  ]

  return (
    <Card
      title="Authentication"
      subtitle="How Sutr authenticates to the upstream API. The credential itself is entered later, when you connect the integration — it is stored server-side and never shown to the agent."
    >
      <div style={{ padding: '18px 24px 4px' }}>
        {schemes.length > 0 && (
          <div style={{ ...bannerStyle('info'), marginBottom: 16 }}>
            <KeyRound size={13} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              The spec declares{' '}
              <strong>
                {schemes.map((scheme) => scheme.type ?? 'unknown').join(', ')}
              </strong>
              . Sutr pre-filled the fields below from it — adjust only if the API documentation says
              otherwise.
            </span>
          </div>
        )}

        <Field label="Preset">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
            {presets.map((preset) => (
              <ChoiceButton
                key={preset.label}
                active={tokenHeader === preset.header && tokenFormat === preset.format}
                onClick={() => {
                  onTokenHeader(preset.header)
                  onTokenFormat(preset.format)
                }}
                label={preset.label}
                hint={preset.header ? `${preset.header}` : 'no header'}
              />
            ))}
          </div>
        </Field>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.2fr', gap: 10 }}>
          <Field
            label="Header name"
            help="The HTTP header the API expects the credential in. Case-insensitive."
            examples={['Authorization', 'X-API-Key', 'apikey']}
          >
            <input
              value={tokenHeader}
              onChange={(event) => onTokenHeader(event.target.value)}
              placeholder="Authorization"
              spellCheck={false}
              style={monoInputStyle}
            />
          </Field>
          <Field
            label="Header value format"
            help="Must contain {token}, which Sutr replaces with the stored credential at call time. Leave both fields empty for an API that needs no auth."
            examples={['Bearer {token}', '{token}', 'Basic {token}', 'token {token}']}
          >
            <input
              value={tokenFormat}
              onChange={(event) => onTokenFormat(event.target.value)}
              placeholder="Bearer {token}"
              spellCheck={false}
              style={monoInputStyle}
            />
          </Field>
        </div>

        {tokenHeader.trim() ? (
          <div style={authPreviewStyle}>
            <KeyRound size={12} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
            <span style={{ color: 'var(--syn-tag)' }}>{tokenHeader}</span>
            <span style={{ color: 'var(--syn-comment)' }}>:</span>
            <span style={{ color: 'var(--syn-string)' }}>
              {tokenFormat.replace('{token}', 'sk_live_•••••') || '(empty)'}
            </span>
          </div>
        ) : (
          <p style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>
            No header configured — tool calls will be sent unauthenticated.
          </p>
        )}
        {tokenHeader.trim() && !tokenFormat.includes('{token}') && (
          <p style={{ fontSize: 11.5, color: 'var(--badge-amber-text)', marginTop: 6 }}>
            The format needs a <code>{'{token}'}</code> placeholder, or the credential will never be
            sent.
          </p>
        )}
      </div>

      <Footer hint="Next: pick the base URL and build the tools." onBack={onBack}>
        <Button size="sm" onClick={onNext}>
          <NextLabel>Build</NextLabel>
        </Button>
      </Footer>
    </Card>
  )
}

// ── Stage 6: build & deploy ─────────────────────────────────────────────────

function BuildStep({
  project,
  selectedServer,
  serverChoice,
  onServerChoice,
  customServerUrl,
  onCustomServerUrl,
  serverVariables,
  onServerVariable,
  integrationName,
  onIntegrationName,
  selectedCount,
  preview,
  created,
  busy,
  onPreview,
  onCreate,
  onDownload,
  onBack,
  onOpenIntegration,
  onDeploy,
}: {
  project: OpenApiProject
  selectedServer: { url: string; variables?: Record<string, { default?: string; enum?: string[] }> } | null
  serverChoice: string
  onServerChoice: (choice: string) => void
  customServerUrl: string
  onCustomServerUrl: (value: string) => void
  serverVariables: Record<string, string>
  onServerVariable: (key: string, value: string) => void
  integrationName: string
  onIntegrationName: (value: string) => void
  selectedCount: number
  preview: OpenApiCompileResult | null
  created: OpenApiCompileResult | null
  busy: boolean
  onPreview: () => void
  onCreate: () => void
  onDownload: () => void
  onBack: () => void
  onOpenIntegration: () => void
  onDeploy: () => void
}) {
  if (created) {
    return (
      <Card
        title="Integration created"
        subtitle={`${created.tools.length} tool${created.tools.length === 1 ? '' : 's'} are registered and ready to govern.`}
      >
        <div style={{ padding: '18px 24px' }}>
          <div style={{ ...bannerStyle('success'), marginBottom: 14 }}>
            <Check size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              Every tool starts in <strong>require approval</strong> mode. Connect the credential
              and set per-tool policy on the integration page.
            </span>
          </div>
          <ol style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.9 }}>
            <li>Connect the API credential, then relax policy per tool.</li>
            <li>Try a call in the Playground.</li>
            <li>Optionally deploy it as a standalone MCP server - next stage.</li>
          </ol>
        </div>
        <Footer hint={`Base URL: ${created.base_url}`}>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button size="sm" variant="outline" onClick={onDownload} disabled={busy}>
              {busy ? <Loader2 size={13} style={spin} /> : <><Download size={13} style={{ marginRight: 5 }} /> Server package</>}
            </Button>
            <Button size="sm" variant="outline" onClick={onDeploy}>
              <Rocket size={13} style={{ marginRight: 5 }} /> Deploy
            </Button>
            <Button size="sm" onClick={onOpenIntegration}>
              <NextLabel>Open integration</NextLabel>
            </Button>
          </div>
        </Footer>
      </Card>
    )
  }

  return (
    <Card
      title="Build"
      subtitle="Where the calls go, and what the integration is called. Preview first — nothing is created until you say so."
    >
      <div style={{ padding: '18px 24px 4px' }}>
        <Field
          label="Base URL"
          help="Every tool path is appended to this. Taken from the spec's servers block; override it to point at a different environment (sandbox vs production). Private and loopback addresses are refused."
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {(project.servers ?? []).map((server) => (
              <label key={server.url} style={radioRowStyle}>
                <input
                  type="radio"
                  name="server"
                  checked={serverChoice === server.url}
                  onChange={() => onServerChoice(server.url)}
                />
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5 }}>{server.url}</span>
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
                onChange={() => onServerChoice('custom')}
              />
              <span style={{ fontSize: 12.5 }}>Custom base URL</span>
            </label>
            {serverChoice === 'custom' && (
              <input
                value={customServerUrl}
                onChange={(event) => onCustomServerUrl(event.target.value)}
                placeholder="https://api.example.com/v1"
                spellCheck={false}
                style={monoInputStyle}
              />
            )}
            {selectedServer?.variables && Object.keys(selectedServer.variables).length > 0 && (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
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
                        onChange={(event) => onServerVariable(key, event.target.value)}
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
                        onChange={(event) => onServerVariable(key, event.target.value)}
                        style={{ ...inputStyle, height: 32 }}
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
          </div>
        </Field>

        <Field label="Integration name" help="Shown in the catalog and to agents.">
          <input
            value={integrationName}
            onChange={(event) => onIntegrationName(event.target.value)}
            style={inputStyle}
          />
        </Field>

        {preview && (
          <>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                textTransform: 'uppercase',
                letterSpacing: 0.5,
                color: 'var(--text)',
                margin: '4px 0 8px',
              }}
            >
              {preview.tools.length} tool{preview.tools.length === 1 ? '' : 's'} · calls go to{' '}
              <code style={{ fontWeight: 400 }}>{preview.base_url}</code>
            </div>
            <div
              style={{
                maxHeight: 240,
                overflow: 'auto',
                border: '1px solid var(--border)',
                borderRadius: 8,
                marginBottom: 12,
              }}
            >
              {preview.tools.map((tool) => (
                <div
                  key={tool.name}
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    gap: 10,
                    padding: '6px 10px',
                    borderBottom: '1px solid var(--border)',
                    fontSize: 12,
                  }}
                >
                  <span
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontWeight: 600,
                      color: 'var(--text)',
                    }}
                  >
                    {tool.name}
                  </span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-dim)' }}>
                    {tool.method} {tool.path}
                  </span>
                  {tool.renamed_from && (
                    <span style={{ fontSize: 10.5, color: 'var(--text-faint)' }}>
                      renamed from {tool.renamed_from} (name collision)
                    </span>
                  )}
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                    {tool.param_count} param{tool.param_count === 1 ? '' : 's'}
                  </span>
                </div>
              ))}
            </div>
            {preview.warnings.length > 0 && <WarningList warnings={preview.warnings} inline />}
          </>
        )}
      </div>

      <Footer
        hint={
          preview
            ? 'Creating registers the tools; the package is a standalone server you run yourself.'
            : `${selectedCount} operation${selectedCount === 1 ? '' : 's'} selected — preview to see the exact tools.`
        }
        onBack={onBack}
      >
        <div style={{ display: 'flex', gap: 8 }}>
          <Button
            size="sm"
            variant={preview ? 'outline' : 'default'}
            onClick={onPreview}
            disabled={busy}
          >
            {busy && !preview ? <Loader2 size={13} style={spin} /> : preview ? 'Refresh' : 'Preview tools'}
          </Button>
          {preview && (
            <>
              <Button size="sm" variant="outline" onClick={onDownload} disabled={busy}>
                <Download size={13} style={{ marginRight: 5 }} /> Package
              </Button>
              <Button size="sm" variant="outline" onClick={onDeploy} disabled={busy}>
                <Rocket size={13} style={{ marginRight: 5 }} /> Deploy
              </Button>
              <Button size="sm" onClick={onCreate} disabled={busy}>
                {busy ? <Loader2 size={13} style={spin} /> : <NextLabel>Create integration</NextLabel>}
              </Button>
            </>
          )}
        </div>
      </Footer>
    </Card>
  )
}

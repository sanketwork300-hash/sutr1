import { useState } from 'react'
import { ArrowRight } from 'lucide-react'

/**
 * The real compile path, stage by stage. Every description here corresponds to
 * something the server does: import parses and normalises the document,
 * compile derives tool definitions, policy is per-tool execution mode, deploy
 * packages a container, and the result is an MCP endpoint.
 */
const STAGES = [
  {
    id: 'openapi',
    label: 'OpenAPI',
    title: 'Import a specification',
    body: 'Upload a file, paste a document, point at a URL, or pull it straight from a GitHub repository or SwaggerHub. OpenAPI 3.x in, validated on the way through.',
    detail: [
      'source: upload · url · github · swaggerhub · paste',
      'validation: structural + reference resolution',
    ],
  },
  {
    id: 'normalize',
    label: 'Normalize',
    title: 'Normalize into one representation',
    body: 'References are resolved, servers and variables are pinned, security schemes are read, and every operation is reduced to a shape the compiler can reason about.',
    detail: ['operations · parameters · schemas', 'servers · security schemes · tags'],
  },
  {
    id: 'compile',
    label: 'Compile',
    title: 'Compile operations into tools',
    body: 'Each selected operation becomes a named tool with a JSON Schema for its arguments. Collisions are renamed rather than dropped, and the warnings tell you what changed.',
    detail: ['GET /customers/{id} → get_customer', 'POST /payments → create_payment'],
  },
  {
    id: 'govern',
    label: 'Govern',
    title: 'Attach a policy to every tool',
    body: 'A tool executes automatically, asks a human first, or is denied outright. The policy is stored per tool and evaluated on every call — not advisory metadata.',
    detail: ['auto approve · ask · deny', 'approval binds to the exact arguments'],
  },
  {
    id: 'deploy',
    label: 'Deploy',
    title: 'Package and deploy',
    body: 'The compiled server is packaged into a container and deployed to the target you choose. The image is snapshotted at build time, so the deployment is reproducible.',
    detail: [
      'docker · Cloud Run · Container Apps · App Runner',
      'secrets injected at runtime, never baked in',
    ],
  },
  {
    id: 'mcp',
    label: 'Expose MCP',
    title: 'Expose a governed MCP endpoint',
    body: 'Agents connect over Streamable HTTP. Every call is authenticated, checked against policy, metered and written to the audit log before it reaches your API.',
    detail: ['transport: streamable http', 'auth: OAuth 2.1 or API key'],
  },
]

export function PipelineSection() {
  const [active, setActive] = useState(0)
  const stage = STAGES[active]

  return (
    <section className="lp__section" id="platform">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">The pipeline</span>
          <h2 className="sutr-h1">From API specification to governed agent capability.</h2>
          <p className="sutr-lead">
            One path, six stages, no hand-written glue. Select a stage to see what happens inside
            it.
          </p>
        </div>

        <div className="lp__stages" role="tablist" aria-label="Compile pipeline stages">
          {STAGES.map((item, index) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={index === active}
              aria-controls="pipeline-detail"
              className="lp__stage"
              onClick={() => setActive(index)}
            >
              <span className="lp__stage-index">{String(index + 1).padStart(2, '0')}</span>
              {item.label}
            </button>
          ))}
        </div>

        <div className="lp__panel" id="pipeline-detail" role="tabpanel">
          <div className="lp__panel-bar">
            stage {String(active + 1).padStart(2, '0')} / {STAGES.length}
            <ArrowRight size={12} style={{ marginLeft: 4 }} />
            <span style={{ color: 'var(--brand)' }}>{stage.label}</span>
          </div>
          <div className="lp__panel-body">
            <div className="lp__split" style={{ gap: 32, alignItems: 'flex-start' }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <h3 className="sutr-h2" style={{ fontSize: 20 }}>
                  {stage.title}
                </h3>
                <p className="sutr-body">{stage.body}</p>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {stage.detail.map((line) => (
                  <div key={line} className="lp__op">
                    <span style={{ color: 'var(--brand)' }}>›</span>
                    <span className="sutr-truncate" title={line}>
                      {line}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

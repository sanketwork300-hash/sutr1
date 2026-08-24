import { ArrowRight, Check } from 'lucide-react'

const STEPS = [
  { n: 1, label: 'Source', note: 'upload · url · github · swaggerhub' },
  { n: 2, label: 'Normalize & IR', note: 'operations, servers, security' },
  { n: 3, label: 'Select tools', note: 'per-operation, with risk' },
  { n: 4, label: 'Authentication', note: 'header, format, credential source' },
  { n: 5, label: 'Build', note: 'compile · validate · package' },
  { n: 6, label: 'Deploy', note: 'container to your target' },
]

const OPERATIONS = [
  { method: 'GET', path: '/customers/{id}', tool: 'get_customer', risk: 'low' },
  { method: 'POST', path: '/payments', tool: 'create_payment', risk: 'high' },
  { method: 'GET', path: '/orders', tool: 'list_orders', risk: 'low' },
]

/**
 * A miniature of the real builder. The stage names, the naming convention and
 * the risk derivation are the same ones the console uses — this is a preview
 * of the product, not an illustration of an idea.
 */
export function BuilderSection() {
  return (
    <section className="lp__section">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">MCP builder</span>
          <h2 className="sutr-h1">Six stages from a document to a running server.</h2>
          <p className="sutr-lead">
            The builder never asks you to retype what the specification already says. It reads the
            document, shows you what it found, and lets you decide what an agent is allowed to
            reach.
          </p>
        </div>

        <div className="lp__split" style={{ alignItems: 'stretch' }}>
          <div className="lp__panel">
            <div className="lp__panel-bar">builder stages</div>
            <div
              className="lp__panel-body"
              style={{ display: 'flex', flexDirection: 'column', gap: 2 }}
            >
              {STEPS.map((step, index) => (
                <div
                  key={step.n}
                  style={{
                    display: 'flex',
                    gap: 12,
                    alignItems: 'flex-start',
                    padding: '9px 0',
                    borderBottom: index === STEPS.length - 1 ? 'none' : '1px solid var(--border)',
                  }}
                >
                  <span
                    style={{
                      width: 20,
                      height: 20,
                      borderRadius: 5,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      background: index < 3 ? 'var(--brand-soft)' : 'var(--neutral-soft)',
                      color: index < 3 ? 'var(--brand)' : 'var(--text-faint)',
                      fontFamily: 'var(--font-mono)',
                      fontSize: 10,
                      fontWeight: 600,
                      flexShrink: 0,
                      marginTop: 1,
                    }}
                  >
                    {index < 3 ? <Check size={11} /> : step.n}
                  </span>
                  <span style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                    <span style={{ fontSize: 13, fontWeight: 550, color: 'var(--text)' }}>
                      {step.label}
                    </span>
                    <span
                      className="sutr-mono"
                      style={{ color: 'var(--text-faint)', fontSize: 11 }}
                    >
                      {step.note}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div className="lp__panel">
              <div className="lp__panel-bar">operations → tools</div>
              <div className="lp__panel-body">
                <div className="lp__transform">
                  <div>
                    {OPERATIONS.map((op) => (
                      <div key={op.path} className="lp__op">
                        <span
                          className="lp__op-method"
                          style={{
                            color: op.method === 'GET' ? 'var(--blue)' : 'var(--amber)',
                          }}
                        >
                          {op.method}
                        </span>
                        <span className="sutr-truncate">{op.path}</span>
                      </div>
                    ))}
                  </div>
                  <div
                    className="lp__transform-arrow"
                    style={{
                      display: 'flex',
                      justifyContent: 'center',
                      color: 'var(--text-faint)',
                    }}
                  >
                    <ArrowRight size={16} />
                  </div>
                  <div>
                    {OPERATIONS.map((op) => (
                      <div
                        key={op.tool}
                        className="lp__op"
                        style={{ justifyContent: 'space-between' }}
                      >
                        <span style={{ color: 'var(--text)' }}>{op.tool}</span>
                        <span
                          style={{
                            fontSize: 10,
                            letterSpacing: '0.06em',
                            textTransform: 'uppercase',
                            color: op.risk === 'high' ? 'var(--amber)' : 'var(--text-faint)',
                          }}
                        >
                          {op.risk}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>

            <div className="lp__panel">
              <div className="lp__panel-bar">result</div>
              <div
                className="lp__panel-body"
                style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
              >
                <span className="sutr-eyebrow">MCP endpoint</span>
                <code
                  className="sutr-mono"
                  style={{
                    color: 'var(--text)',
                    background: 'var(--code-bg)',
                    border: '1px solid var(--border)',
                    borderRadius: 'var(--r-sm)',
                    padding: '8px 10px',
                    display: 'block',
                    overflowX: 'auto',
                    whiteSpace: 'nowrap',
                  }}
                >
                  https://your-sutr-instance/mcp
                </code>
                <span className="sutr-meta">
                  3 tools · streamable HTTP · credential injected server-side
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

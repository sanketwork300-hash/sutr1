import { useState } from 'react'
import { Check, ShieldAlert, ShieldCheck, ShieldX } from 'lucide-react'
import { SutrBadge, SutrButton } from '@/components/sutr'

const CHAIN = ['Agent', 'Identity', 'Authorization', 'Policy', 'Approval', 'Execution']

const MODES = [
  {
    id: 'allow',
    icon: ShieldCheck,
    label: 'AUTO APPROVE',
    color: 'var(--green)',
    body: 'The tool runs immediately. Still authenticated, still metered, still written to the audit log.',
  },
  {
    id: 'ask',
    icon: ShieldAlert,
    label: 'ASK',
    color: 'var(--amber)',
    body: 'Execution halts until a person decides. The agent receives a typed approval-required outcome, not an error.',
  },
  {
    id: 'deny',
    icon: ShieldX,
    label: 'DENY',
    color: 'var(--red)',
    body: 'The call never reaches the provider. Waiting will not help, and the agent is told so plainly.',
  },
]

type Phase = 'idle' | 'pending' | 'approved'

export function GovernanceSection() {
  const [phase, setPhase] = useState<Phase>('idle')

  return (
    <section className="lp__section" id="governance">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">Governance</span>
          <h2 className="sutr-h1">Every tool call has a policy.</h2>
          <p className="sutr-lead">
            Authorization is not a prompt instruction the model may ignore. It is a decision the
            gateway makes before the request leaves your infrastructure.
          </p>
        </div>

        <div
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            gap: 8,
            marginBottom: 30,
          }}
        >
          {CHAIN.map((step, index) => (
            <span key={step} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <span className="lp__chip">{step}</span>
              {index < CHAIN.length - 1 ? (
                <span style={{ color: 'var(--text-faint)' }}>→</span>
              ) : null}
            </span>
          ))}
        </div>

        <div className="lp__split" style={{ alignItems: 'stretch' }}>
          <div className="lp__grid-2" style={{ gridTemplateColumns: 'minmax(0,1fr)', gap: 12 }}>
            {MODES.map((mode) => {
              const Icon = mode.icon
              return (
                <div key={mode.id} className="lp__feature" style={{ padding: 16 }}>
                  <span
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 8,
                      color: mode.color,
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      letterSpacing: '0.1em',
                    }}
                  >
                    <Icon size={14} />
                    {mode.label}
                  </span>
                  <p className="sutr-body" style={{ fontSize: 12.5 }}>
                    {mode.body}
                  </p>
                </div>
              )
            })}
          </div>

          <div className="lp__panel">
            <div className="lp__panel-bar">
              approval flow
              <span style={{ marginLeft: 'auto', textTransform: 'none', letterSpacing: 0 }}>
                demonstration — nothing is sent
              </span>
            </div>
            <div
              className="lp__panel-body"
              style={{ display: 'flex', flexDirection: 'column', gap: 14 }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <code className="sutr-mono" style={{ fontSize: 14, color: 'var(--text)' }}>
                  create_refund
                </code>
                {phase === 'approved' ? (
                  <SutrBadge tone="success" dot>
                    APPROVED
                  </SutrBadge>
                ) : phase === 'pending' ? (
                  <SutrBadge tone="warning" dot live>
                    APPROVAL REQUIRED
                  </SutrBadge>
                ) : (
                  <SutrBadge tone="neutral" dot>
                    POLICY: ASK
                  </SutrBadge>
                )}
              </div>

              <pre
                className="sutr-code__pre"
                style={{
                  background: 'var(--code-bg)',
                  border: '1px solid var(--border)',
                  borderRadius: 'var(--r-sm)',
                  padding: 12,
                  margin: 0,
                }}
              >
                {`{\n  "charge": "ch_3PtY...",\n  "amount": 500000,\n  "currency": "usd"\n}`}
              </pre>

              {phase === 'approved' ? (
                <div
                  style={{
                    display: 'flex',
                    gap: 10,
                    padding: 12,
                    borderRadius: 'var(--r-sm)',
                    border: '1px solid color-mix(in srgb, var(--green) 30%, transparent)',
                    background: 'var(--success-soft)',
                  }}
                >
                  <Check size={15} style={{ color: 'var(--green)', flexShrink: 0, marginTop: 1 }} />
                  <span style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <span style={{ fontSize: 12.5, color: 'var(--text)', fontWeight: 550 }}>
                      Exact arguments authorized
                    </span>
                    <span className="sutr-meta">
                      The grant is bound to this argument hash. A different amount, a different
                      charge, or a second attempt needs its own decision.
                    </span>
                  </span>
                </div>
              ) : (
                <p className="sutr-meta">
                  A person sees the exact arguments before deciding. Approving authorizes this
                  request — not the tool, and not the next call.
                </p>
              )}

              <div style={{ display: 'flex', gap: 8 }}>
                {phase === 'idle' ? (
                  <SutrButton variant="brand" onClick={() => setPhase('pending')}>
                    Request approval
                  </SutrButton>
                ) : phase === 'pending' ? (
                  <>
                    <SutrButton variant="brand" onClick={() => setPhase('approved')}>
                      Approve exact request
                    </SutrButton>
                    <SutrButton variant="danger" onClick={() => setPhase('idle')}>
                      Deny
                    </SutrButton>
                  </>
                ) : (
                  <SutrButton variant="secondary" onClick={() => setPhase('idle')}>
                    Replay
                  </SutrButton>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

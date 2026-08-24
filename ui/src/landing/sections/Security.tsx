const CONTROLS = [
  {
    title: 'Fine-grained authorization',
    body: 'Permissions are checked per action against the caller’s role in the organisation. An API key is an identity with a scope, not a skeleton key.',
  },
  {
    title: 'Credentials never leave the server',
    body: 'Upstream tokens live in the secrets backend and are injected into the request server-side. Agent code holds a Sutr key and nothing else.',
  },
  {
    title: 'Approval bound to arguments',
    body: 'An approval authorizes one argument hash. Replaying the grant with different values fails the check rather than sliding through.',
  },
  {
    title: 'Second factor on sensitive decisions',
    body: 'When TOTP is enabled, approving a request or changing a tool policy requires a fresh code — the console session alone is not enough.',
  },
  {
    title: 'SSRF protection on every upstream call',
    body: 'Base URLs and request targets are validated before a connection is opened, so a specification cannot point the gateway at internal addresses.',
  },
  {
    title: 'Auditable by construction',
    body: 'Every execution, approval decision and configuration change is written to the audit log with actor, arguments hash, IP and outcome.',
  },
]

const INSIDE = ['Identity', 'Secrets', 'Tools', 'Policies', 'Deployments']

function BoundaryDiagram() {
  return (
    <svg
      viewBox="0 0 460 250"
      width="100%"
      height="auto"
      role="img"
      aria-label="Agents sit outside the Sutr trust boundary; identity, secrets, tools, policies and deployments sit inside it; upstream providers are reached only from inside."
      style={{ display: 'block' }}
    >
      <rect
        x="10"
        y="14"
        width="200"
        height="34"
        rx="7"
        fill="var(--surface)"
        stroke="var(--border-strong)"
      />
      <text
        x="110"
        y="35"
        textAnchor="middle"
        fontSize="11"
        fontFamily="var(--font-mono)"
        fill="var(--text-dim)"
      >
        AGENTS · UNTRUSTED
      </text>

      <path d="M110,48 L110,72" stroke="var(--border-strong)" strokeWidth="1.2" />

      <rect
        x="8"
        y="72"
        width="444"
        height="118"
        rx="12"
        fill="var(--brand-soft)"
        stroke="var(--brand-border)"
        strokeDasharray="5 4"
      />
      <text
        x="24"
        y="92"
        fontSize="10"
        letterSpacing="1.4"
        fontFamily="var(--font-mono)"
        fill="var(--brand)"
      >
        SUTR TRUST BOUNDARY
      </text>

      {INSIDE.map((label, index) => (
        <g key={label}>
          <rect
            x={24 + index * 86}
            y={108}
            width={76}
            height={30}
            rx={6}
            fill="var(--content-bg)"
            stroke="var(--border)"
          />
          <text
            x={62 + index * 86}
            y={127}
            textAnchor="middle"
            fontSize="10"
            fontFamily="var(--font-mono)"
            fill="var(--text)"
          >
            {label}
          </text>
        </g>
      ))}

      <text x="24" y="164" fontSize="10.5" fontFamily="var(--font-mono)" fill="var(--text-faint)">
        authorize → policy → approve → inject credential → execute → audit
      </text>

      <path d="M230,190 L230,212" stroke="var(--border-strong)" strokeWidth="1.2" />
      <rect
        x="130"
        y="212"
        width="200"
        height="32"
        rx="7"
        fill="var(--surface)"
        stroke="var(--border-strong)"
      />
      <text
        x="230"
        y="232"
        textAnchor="middle"
        fontSize="11"
        fontFamily="var(--font-mono)"
        fill="var(--text-dim)"
      >
        UPSTREAM PROVIDERS
      </text>
    </svg>
  )
}

export function SecuritySection() {
  return (
    <section className="lp__section">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">Security</span>
          <h2 className="sutr-h1">Your agent should never have unrestricted access.</h2>
          <p className="sutr-lead">
            The boundary is the product. Everything an agent can reach is something you granted,
            scoped, and can revoke.
          </p>
        </div>

        <div className="lp__panel" style={{ marginBottom: 24 }}>
          <div className="lp__panel-bar">trust boundary</div>
          <div className="lp__panel-body">
            <BoundaryDiagram />
          </div>
        </div>

        <div className="lp__grid-3">
          {CONTROLS.map((control) => (
            <div key={control.title} className="lp__feature">
              <h3 className="sutr-h3">{control.title}</h3>
              <p className="sutr-body" style={{ fontSize: 12.5 }}>
                {control.body}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

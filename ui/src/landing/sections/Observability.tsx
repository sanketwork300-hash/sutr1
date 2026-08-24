const TRACE = [
  {
    time: '10:42:11.204',
    label: 'Agent request',
    detail: 'api key ap_7f2… · mcp/streamable-http',
    tone: 'brand',
  },
  {
    time: '10:42:11.207',
    label: 'Authorization',
    detail: 'org member · tools:call granted',
    tone: 'success',
  },
  {
    time: '10:42:11.209',
    label: 'Policy evaluated',
    detail: 'get_customer → auto approve',
    tone: 'success',
  },
  {
    time: '10:42:11.213',
    label: 'Credential injected',
    detail: 'secret ref · never returned to the agent',
    tone: 'neutral',
  },
  {
    time: '10:42:11.402',
    label: 'Upstream call',
    detail: 'GET /customers/{id} → 200',
    tone: 'success',
  },
  {
    time: '10:42:11.404',
    label: 'Result returned',
    detail: '198 ms · metered · written to audit log',
    tone: 'success',
  },
]

const SIGNALS = [
  { key: 'Latency', value: 'per call, upstream and total' },
  { key: 'Status', value: 'executed · approval required · denied · error' },
  { key: 'Tool', value: 'name and integration' },
  { key: 'Caller', value: 'user, API key label, IP, user agent' },
  { key: 'Arguments', value: 'hash on every call, full arguments on approval' },
  { key: 'Traces', value: 'OpenTelemetry spans when a collector is configured' },
]

export function ObservabilitySection() {
  return (
    <section className="lp__section">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">Observability</span>
          <h2 className="sutr-h1">See every agent action.</h2>
          <p className="sutr-lead">
            Not a log stream you have to grep. An execution timeline that shows what the agent asked
            for, what the policy decided, and what your API answered.
          </p>
        </div>

        <div className="lp__split" style={{ alignItems: 'flex-start' }}>
          <div className="lp__panel">
            <div className="lp__panel-bar">execution trace</div>
            <div className="lp__panel-body">
              <div className="sutr-timeline">
                {TRACE.map((entry) => (
                  <div key={entry.time} className="sutr-timeline__item">
                    <span className="sutr-timeline__time">{entry.time.slice(0, 8)}</span>
                    <span className="sutr-timeline__spine" aria-hidden="true">
                      <span
                        className={`sutr-timeline__node${
                          entry.tone === 'neutral' ? '' : ` sutr-timeline__node--${entry.tone}`
                        }`}
                      />
                    </span>
                    <span className="sutr-timeline__body">
                      <span className="sutr-timeline__title">{entry.label}</span>
                      <span className="sutr-meta sutr-mono">{entry.detail}</span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <h3 className="sutr-h2" style={{ fontSize: 20 }}>
              What each execution records
            </h3>
            <dl className="sutr-dl">
              {SIGNALS.map((signal) => (
                <div key={signal.key} style={{ display: 'contents' }}>
                  <dt className="sutr-dl__key">{signal.key}</dt>
                  <dd className="sutr-dl__val" style={{ margin: 0, color: 'var(--text-dim)' }}>
                    {signal.value}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="sutr-meta">
              Usage is metered per organisation and exposed through the console, the CLI and a
              Prometheus endpoint.
            </p>
          </div>
        </div>
      </div>
    </section>
  )
}

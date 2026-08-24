import { Boxes, Cloud, Container, Server } from 'lucide-react'

/**
 * Exactly the four providers implemented in `server/src/sutr/deploy/`, with
 * the pipeline each one actually runs. The console derives its own list from
 * `/api/deployments/providers`, which additionally reports whether a provider
 * is configured on this instance — nothing is promised here that the server
 * cannot do.
 */
const PROVIDERS = [
  {
    id: 'docker',
    icon: Container,
    name: 'Your own host',
    target: 'Docker on the Sutr host',
    pipeline: ['build image', 'run container', 'bind to loopback'],
  },
  {
    id: 'gcp',
    icon: Cloud,
    name: 'Google Cloud',
    target: 'Cloud Run',
    pipeline: ['GCS upload', 'Cloud Build', 'Artifact Registry', 'Cloud Run'],
  },
  {
    id: 'azure',
    icon: Cloud,
    name: 'Microsoft Azure',
    target: 'Container Apps',
    pipeline: ['ACR source upload', 'ACR Task', 'Container Apps'],
  },
  {
    id: 'aws',
    icon: Cloud,
    name: 'Amazon Web Services',
    target: 'App Runner',
    pipeline: ['S3', 'CodeBuild', 'ECR', 'App Runner'],
  },
]

const SOVEREIGN = [
  {
    icon: Server,
    title: 'Runs where you run',
    body: 'A single container image, deployed to the host or the account you already operate. No traffic detours through a vendor you did not choose.',
  },
  {
    icon: Boxes,
    title: 'Runtime isolation',
    body: 'Each generated MCP server is its own container with its own credential. Compromising one tells you nothing about the next.',
  },
  {
    icon: Container,
    title: 'Secrets injected at runtime',
    body: 'The token is stored in the secrets backend and passed as an environment variable at start. It is never baked into the image.',
  },
]

export function DeploySection() {
  return (
    <section className="lp__section" id="deploy">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">Deployment</span>
          <h2 className="sutr-h1">Build once. Deploy where you run.</h2>
          <p className="sutr-lead">
            The same package goes to your own host or into your own cloud account, authorized by a
            connected account you control and revocable at any time.
          </p>
        </div>

        <div className="lp__grid-2" style={{ marginBottom: 34 }}>
          {PROVIDERS.map((provider) => {
            const Icon = provider.icon
            return (
              <div key={provider.id} className="lp__feature">
                <span className="lp__feature-icon">
                  <Icon size={15} />
                </span>
                <h3 className="sutr-h3">{provider.name}</h3>
                <span className="sutr-mono" style={{ color: 'var(--text-dim)' }}>
                  {provider.target}
                </span>
                <div
                  style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 6,
                    marginTop: 6,
                    alignItems: 'center',
                  }}
                >
                  {provider.pipeline.map((step, index) => (
                    <span
                      key={step}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
                    >
                      <span
                        className="sutr-mono"
                        style={{
                          fontSize: 10.5,
                          color: 'var(--text-faint)',
                          border: '1px solid var(--border)',
                          borderRadius: 4,
                          padding: '2px 6px',
                        }}
                      >
                        {step}
                      </span>
                      {index < provider.pipeline.length - 1 ? (
                        <span style={{ color: 'var(--text-faint)', fontSize: 11 }}>→</span>
                      ) : null}
                    </span>
                  ))}
                </div>
              </div>
            )
          })}
        </div>

        <div className="lp__panel">
          <div className="lp__panel-bar">sovereignty</div>
          <div className="lp__panel-body">
            <div className="lp__section-head" style={{ marginBottom: 26 }}>
              <h3 className="sutr-h2">Swaraj: the infrastructure answers to you.</h3>
              <p className="sutr-body">
                Sutr is self-hostable end to end — server, database, secrets backend and every MCP
                server it generates. Cloud providers are optional targets you authorize with your
                own account, not a dependency the platform imposes.
              </p>
            </div>

            <div className="lp__grid-3">
              {SOVEREIGN.map((item) => {
                const Icon = item.icon
                return (
                  <div
                    key={item.title}
                    style={{ display: 'flex', flexDirection: 'column', gap: 7 }}
                  >
                    <span className="lp__feature-icon">
                      <Icon size={15} />
                    </span>
                    <h4 className="sutr-h3">{item.title}</h4>
                    <p className="sutr-body" style={{ fontSize: 12.5 }}>
                      {item.body}
                    </p>
                  </div>
                )
              })}
            </div>

            <div
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                gap: 8,
                marginTop: 26,
                paddingTop: 18,
                borderTop: '1px solid var(--border)',
              }}
            >
              {['Your API', 'Sutr', 'Container', 'Your infrastructure', 'MCP endpoint'].map(
                (step, index, all) => (
                  <span key={step} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                    <span className="lp__chip">{step}</span>
                    {index < all.length - 1 ? (
                      <span style={{ color: 'var(--text-faint)' }}>→</span>
                    ) : null}
                  </span>
                ),
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

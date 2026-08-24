import { Link } from 'react-router-dom'
import { BrandLockup } from '@/components/shell/BrandMark'
import { SutrLinkButton, SutrExternalButton } from '@/components/sutr'

const GITHUB_URL = 'https://github.com/sutr-dev'
const SKILLS_URL = 'https://github.com/sutr-dev/sutr-skills'

export function LandingNav({ signedIn }: { signedIn: boolean }) {
  return (
    <div className="lp__nav">
      <nav className="lp__nav-inner" aria-label="Primary">
        <Link to="/" aria-label="Swaraj Sutr home" style={{ textDecoration: 'none' }}>
          <BrandLockup size={19} />
        </Link>

        <div className="lp__nav-links">
          <a className="lp__nav-link" href="#platform">
            Platform
          </a>
          <a className="lp__nav-link" href="#governance">
            Governance
          </a>
          <a className="lp__nav-link" href="#deploy">
            Deployment
          </a>
          <a className="lp__nav-link" href="#developers">
            Developers
          </a>
          <a className="lp__nav-link" href="/docs">
            API reference
          </a>
        </div>

        <div className="lp__nav-actions">
          {signedIn ? (
            <SutrLinkButton to="/app" variant="brand" size="sm">
              Open console
            </SutrLinkButton>
          ) : (
            <>
              <SutrLinkButton to="/login" variant="ghost" size="sm">
                Sign in
              </SutrLinkButton>
              <SutrLinkButton to="/signup" variant="brand" size="sm">
                Start building
              </SutrLinkButton>
            </>
          )}
        </div>
      </nav>
    </div>
  )
}

export function FinalCta({ signedIn }: { signedIn: boolean }) {
  return (
    <section className="lp__section lp__section--flush">
      <div className="lp__shell">
        <div className="lp__cta">
          <h2 className="sutr-h1" style={{ maxWidth: 660 }}>
            Give your agents infrastructure they can actually trust.
          </h2>
          <p className="sutr-lead" style={{ maxWidth: 560 }}>
            Import a specification, choose what becomes a tool, set the policy for each one, and
            hand your agent a governed MCP endpoint.
          </p>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', justifyContent: 'center' }}>
            <SutrLinkButton to={signedIn ? '/app' : '/signup'} variant="brand" size="lg">
              {signedIn ? 'Open console' : 'Start building'}
            </SutrLinkButton>
            <SutrExternalButton href="/docs" variant="secondary" size="lg">
              View documentation
            </SutrExternalButton>
          </div>
        </div>
      </div>
    </section>
  )
}

export function LandingFooter() {
  return (
    <footer className="lp__footer">
      <div className="lp__shell">
        <div className="lp__footer-grid">
          <div className="lp__footer-col">
            <BrandLockup size={19} />
            <p className="sutr-meta" style={{ maxWidth: 260, marginTop: 6 }}>
              The infrastructure layer for governed AI agents. Connect APIs, MCP servers and
              enterprise tools; govern every execution.
            </p>
          </div>

          <div className="lp__footer-col">
            <span className="sutr-eyebrow">Platform</span>
            <Link className="lp__footer-link" to="/app">
              Console
            </Link>
            <Link className="lp__footer-link" to="/signup">
              Start building
            </Link>
            <Link className="lp__footer-link" to="/login">
              Sign in
            </Link>
          </div>

          <div className="lp__footer-col">
            <span className="sutr-eyebrow">Developers</span>
            <a className="lp__footer-link" href="/docs">
              API reference
            </a>
            <a
              className="lp__footer-link"
              href={GITHUB_URL}
              target="_blank"
              rel="noreferrer noopener"
            >
              GitHub
            </a>
            <a
              className="lp__footer-link"
              href={SKILLS_URL}
              target="_blank"
              rel="noreferrer noopener"
            >
              Sutr skills
            </a>
          </div>

          <div className="lp__footer-col">
            <span className="sutr-eyebrow">Protocols</span>
            <span className="sutr-meta">Model Context Protocol</span>
            <span className="sutr-meta">OpenAPI 3.x</span>
            <span className="sutr-meta">OAuth 2.1 · OpenTelemetry</span>
          </div>
        </div>

        <hr className="lp__rule" style={{ margin: '34px 0 18px' }} />
        <p className="sutr-meta">Swaraj Sutr — infrastructure sovereignty for agent systems.</p>
      </div>
    </footer>
  )
}

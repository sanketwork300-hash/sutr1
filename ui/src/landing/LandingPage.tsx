import { useEffect } from 'react'
import './landing.css'
import { ArrowRight } from 'lucide-react'
import { HeroGraph } from './HeroGraph'
import { LandingNav, LandingFooter, FinalCta } from './LandingChrome'
import { PipelineSection } from './sections/Pipeline'
import { BuilderSection } from './sections/Builder'
import { GovernanceSection } from './sections/Governance'
import { SecuritySection } from './sections/Security'
import { DeploySection } from './sections/Deploy'
import { ObservabilitySection } from './sections/Observability'
import { DeveloperSection } from './sections/Developer'
import { useAuthStore } from '@/stores/auth'
import { SutrLinkButton, SutrExternalButton } from '@/components/sutr'

/**
 * Capabilities, not customer logos. Every entry is something this build
 * actually speaks — there are no invented users, numbers or testimonials
 * anywhere on this page.
 */
const CAPABILITIES = [
  'MCP native',
  'OpenAPI 3.x',
  'OAuth 2.1',
  'API keys',
  'Container deployments',
  'Fine-grained governance',
  'OpenTelemetry',
  'Self-hostable',
]

export default function LandingPage() {
  const token = useAuthStore((s) => s.token)
  const signedIn = Boolean(token)

  // The console is dark-first by design; the public page inherits whatever the
  // visitor has chosen, and the document title is set for direct arrivals.
  useEffect(() => {
    document.title = 'Swaraj Sutr — Infrastructure for Governed AI Agents'
  }, [])

  return (
    <div className="lp">
      <LandingNav signedIn={signedIn} />

      <main>
        <section className="lp__hero">
          <div className="lp__shell">
            <div className="lp__hero-inner">
              <div className="lp__hero-copy">
                <span className="lp__pill">
                  <span className="sutr-badge__dot sutr-live-dot" aria-hidden="true" />
                  Swaraj — infrastructure sovereignty
                </span>

                <h1 className="sutr-display">The infrastructure layer for governed AI agents.</h1>

                <p className="sutr-lead" style={{ fontSize: 16, maxWidth: 560 }}>
                  Connect APIs, MCP servers and enterprise tools. Govern every execution. Deploy
                  agent infrastructure anywhere.
                </p>

                <div className="lp__hero-ctas">
                  <SutrLinkButton to={signedIn ? '/app' : '/signup'} variant="brand" size="lg">
                    {signedIn ? 'Open console' : 'Start building'}
                  </SutrLinkButton>
                  <a href="#platform" className="sutr-btn sutr-btn--secondary sutr-btn--lg">
                    Explore the platform
                  </a>
                  <SutrExternalButton href="/docs" variant="ghost" size="lg">
                    Read the docs <ArrowRight size={14} />
                  </SutrExternalButton>
                </div>
              </div>

              <div style={{ minWidth: 0 }}>
                <HeroGraph />
              </div>
            </div>
          </div>
        </section>

        <div className="lp__shell">
          <hr className="lp__rule" />
          <div className="lp__strip" aria-label="Platform capabilities">
            {CAPABILITIES.map((capability) => (
              <span key={capability} className="lp__chip">
                {capability}
              </span>
            ))}
          </div>
        </div>

        <PipelineSection />
        <BuilderSection />
        <GovernanceSection />
        <SecuritySection />
        <DeploySection />
        <ObservabilitySection />
        <DeveloperSection />
        <FinalCta signedIn={signedIn} />
      </main>

      <LandingFooter />
    </div>
  )
}

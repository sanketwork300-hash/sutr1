// First, before anything can read the URL: moves a Google sign-in fragment
// out of the address bar and into memory.
import '@/lib/authFragment'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { PostHogProvider } from '@posthog/react'
// Self-hosted so the console renders in its own typeface with no network round
// trip; the mono face is the only font fetched from a CDN.
import '@fontsource-variable/geist/index.css'
import './index.css'
import './components/sutr/sutr.css'
import App from './App'
import { PostHogAuthSync } from '@/analytics/PostHogAuthSync'
import { posthog } from '@/analytics/posthog'

// initialize theme before render
import './stores/theme'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PostHogProvider client={posthog}>
      {/* Analytics start from inside the router (AnalyticsTracker in App),
          once the current route is known not to carry a credential. */}
      <PostHogAuthSync />
      <App />
    </PostHogProvider>
  </StrictMode>,
)

import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { capturePageview, initAnalytics, isAnalyticsEnabled, isTokenBearingRoute } from './posthog'

/**
 * Starts analytics once the router is up, and reports page views by path.
 *
 * Nothing is initialised while the browser is on a route whose URL carries a
 * credential — a sign-in fragment, a reset or invite token, a consent session.
 * On those routes the first page view would otherwise be the URL itself. Once
 * the app moves to an ordinary route the client starts, and from then on
 * every page view is captured by hand with the query and hash removed.
 */
export function AnalyticsTracker() {
  const { pathname } = useLocation()

  useEffect(() => {
    if (isTokenBearingRoute(pathname)) return
    if (!isAnalyticsEnabled()) initAnalytics()
    capturePageview(pathname)
  }, [pathname])

  return null
}

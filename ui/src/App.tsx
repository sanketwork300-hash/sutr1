import { lazy, Suspense, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useParams } from 'react-router-dom'
import { AppShell } from '@/components/shell/AppShell'
import { useConfigStore } from '@/stores/config'

const LandingPage = lazy(() => import('@/landing/LandingPage'))

const LoginPage = lazy(() => import('@/pages/LoginPage'))
const SignupPage = lazy(() => import('@/pages/SignupPage'))
const ForgotPasswordPage = lazy(() => import('@/pages/ForgotPasswordPage'))
const ResetPasswordPage = lazy(() => import('@/pages/ResetPasswordPage'))
const VerifyEmailPage = lazy(() => import('@/pages/VerifyEmailPage'))
const GoogleCallbackPage = lazy(() => import('@/pages/GoogleCallbackPage'))
const ConnectionCallbackPage = lazy(() => import('@/pages/ConnectionCallbackPage'))
const ApprovePage = lazy(() => import('@/pages/ApprovePage'))
const OAuthConsentPage = lazy(() => import('@/pages/OAuthConsentPage'))
const OAuthSuccessPage = lazy(() => import('@/pages/OAuthSuccessPage'))
const JoinPage = lazy(() => import('@/pages/JoinPage'))

const OverviewPage = lazy(() => import('@/pages/OverviewPage'))
const ConnectionsPage = lazy(() => import('@/pages/ConnectionsPage'))
const ConnectionDetailPage = lazy(() => import('@/pages/ConnectionDetailPage'))
const CustomApiBuilderPage = lazy(() => import('@/pages/CustomApiBuilderPage'))
const McpBuilderPage = lazy(() => import('@/pages/McpBuilderPage'))
const ApisPage = lazy(() => import('@/pages/ApisPage'))
const McpServersPage = lazy(() => import('@/pages/McpServersPage'))
const ToolsPage = lazy(() => import('@/pages/ToolsPage'))
const DeploymentsPage = lazy(() => import('@/pages/DeploymentsPage'))
const UsagePage = lazy(() => import('@/pages/UsagePage'))
const ApprovalsPage = lazy(() => import('@/pages/ApprovalsPage'))
const ActivityPage = lazy(() => import('@/pages/ActivityPage'))
const PoliciesPage = lazy(() => import('@/pages/PoliciesPage'))
const AccessPage = lazy(() => import('@/pages/AccessPage'))
const CredentialsPage = lazy(() => import('@/pages/CredentialsPage'))
const ApiKeysPage = lazy(() => import('@/pages/ApiKeysPage'))
const SdkPage = lazy(() => import('@/pages/SdkPage'))
const CliPage = lazy(() => import('@/pages/CliPage'))
const DeveloperPage = lazy(() => import('@/pages/DeveloperPage'))
const SettingsPage = lazy(() => import('@/pages/SettingsPage'))
const BillingPage = lazy(() => import('@/pages/BillingPage'))
const AdminPage = lazy(() => import('@/pages/AdminPage'))
const PlaygroundPage = lazy(() => import('@/pages/PlaygroundPage'))

/**
 * Console routes moved under /app when the landing page took over `/`.
 * Everything the server links to — OAuth returns, billing redirects, invite
 * mails — still points at the old paths, so those keep resolving here rather
 * than 404ing.
 */
function LegacyRedirect({ to }: { to: string }) {
  const { search, hash } = useLocation()
  return <Navigate to={`${to}${search}${hash}`} replace />
}

function LegacyIntegrationRedirect() {
  const { integrationId } = useParams()
  const { search, hash } = useLocation()
  return (
    <Navigate
      to={`/app/integrations/${encodeURIComponent(integrationId ?? '')}${search}${hash}`}
      replace
    />
  )
}

function LegacyCustomApiRedirect() {
  const { integrationDbId } = useParams()
  const { search, hash } = useLocation()
  return (
    <Navigate
      to={`/app/integrations/custom-api/${encodeURIComponent(integrationDbId ?? '')}${search}${hash}`}
      replace
    />
  )
}

const RouteFallback = (
  <div
    style={{
      height: '100dvh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      color: 'var(--text-faint)',
      fontSize: 13,
    }}
  >
    Loading…
  </div>
)

export default function App() {
  const isSelfHosted = useConfigStore((s) => s.isSelfHosted)
  const fetchConfig = useConfigStore((s) => s.fetch)

  useEffect(() => {
    fetchConfig()
  }, [fetchConfig])

  return (
    <BrowserRouter>
      <Suspense fallback={RouteFallback}>
        <Routes>
          {/* ── Public ── */}
          <Route path="/" element={<LandingPage />} />

          {/* ── Authentication and server-initiated returns ── */}
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/verify-email" element={<VerifyEmailPage />} />
          <Route path="/login/google/callback" element={<GoogleCallbackPage />} />
          <Route path="/connections/callback" element={<ConnectionCallbackPage />} />
          <Route path="/approve/:id" element={<ApprovePage />} />
          <Route path="/oauth/authorize" element={<OAuthConsentPage />} />
          <Route path="/oauth/success" element={<OAuthSuccessPage />} />
          <Route path="/join" element={<JoinPage />} />

          {/* ── Console ── */}
          <Route path="/app" element={<AppShell />}>
            <Route index element={<OverviewPage />} />

            <Route path="apis" element={<ApisPage />} />
            <Route path="apis/new" element={<McpBuilderPage />} />
            <Route path="mcp-servers" element={<McpServersPage />} />
            <Route path="tools" element={<ToolsPage />} />

            <Route path="integrations" element={<ConnectionsPage />} />
            <Route
              path="integrations/custom-api/new"
              element={<Navigate to="/app/apis/new" replace />}
            />
            <Route path="integrations/openapi/new" element={<Navigate to="/app/apis/new" replace />} />
            <Route
              path="integrations/custom-api/:integrationDbId"
              element={<CustomApiBuilderPage />}
            />
            <Route path="integrations/:integrationId" element={<ConnectionDetailPage />} />

            <Route path="playground" element={<PlaygroundPage />} />
            <Route path="approvals" element={<ApprovalsPage />} />
            <Route path="activity" element={<ActivityPage />} />
            <Route path="deployments" element={<DeploymentsPage />} />

            <Route path="policies" element={<PoliciesPage />} />
            <Route path="access" element={<AccessPage />} />
            <Route path="credentials" element={<CredentialsPage />} />

            <Route path="api-keys" element={<ApiKeysPage />} />
            <Route path="connect" element={<DeveloperPage />} />
            <Route path="sdk" element={<SdkPage />} />
            <Route path="cli" element={<CliPage />} />

            <Route path="usage" element={<UsagePage />} />
            <Route path="settings" element={<SettingsPage />} />
            {!isSelfHosted && <Route path="settings/billing" element={<BillingPage />} />}
            <Route path="admin" element={<AdminPage />} />
            <Route path="*" element={<Navigate to="/app" replace />} />
          </Route>

          {/* ── Pre-/app links kept alive ── */}
          <Route path="/integrations" element={<LegacyRedirect to="/app/integrations" />} />
          <Route path="/integrations/openapi/new" element={<LegacyRedirect to="/app/apis/new" />} />
          <Route path="/integrations/custom-api/new" element={<LegacyRedirect to="/app/apis/new" />} />
          <Route
            path="/integrations/custom-api/:integrationDbId"
            element={<LegacyCustomApiRedirect />}
          />
          <Route path="/integrations/:integrationId" element={<LegacyIntegrationRedirect />} />
          <Route path="/deployments" element={<LegacyRedirect to="/app/deployments" />} />
          <Route path="/approvals" element={<LegacyRedirect to="/app/approvals" />} />
          <Route path="/activity" element={<LegacyRedirect to="/app/activity" />} />
          <Route path="/usage" element={<LegacyRedirect to="/app/usage" />} />
          <Route path="/connect" element={<LegacyRedirect to="/app/connect" />} />
          <Route path="/playground" element={<LegacyRedirect to="/app/playground" />} />
          <Route path="/settings" element={<LegacyRedirect to="/app/settings" />} />
          <Route path="/settings/billing" element={<LegacyRedirect to="/app/settings/billing" />} />
          <Route path="/admin" element={<LegacyRedirect to="/app/admin" />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}

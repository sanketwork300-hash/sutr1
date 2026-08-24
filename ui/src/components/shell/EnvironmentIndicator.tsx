import { currentEnvironment } from './environment'

export function EnvironmentIndicator() {
  const hostname = typeof window === 'undefined' ? '' : window.location.hostname
  const env = currentEnvironment()
  return (
    <span className="shell__env" data-env={env} title={`Serving from ${hostname}`}>
      <span
        style={{ width: 5, height: 5, borderRadius: '50%', background: 'currentColor' }}
        aria-hidden="true"
      />
      {env}
    </span>
  )
}

/**
 * Shared presentational vocabulary for the MCP builder.
 *
 * Extracted from McpBuilderPage when the deploy stage and the GitHub source
 * panel became their own components: one Card, one Field, one SecretInput, so
 * a wizard assembled from several files still reads as one wizard.
 */

import { ArrowLeft, ArrowRight, KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { OpenApiWarning } from '@/api/client'
import { monoInputStyle } from '@/components/mcp-builder/styles'

export function Card({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <section
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 12,
        boxShadow: 'var(--card-shadow)',
        overflow: 'hidden',
      }}
    >
      <header style={{ padding: '20px 24px 16px', borderBottom: '1px solid var(--border)' }}>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>{title}</h2>
        <p style={{ margin: '4px 0 0', fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>
          {subtitle}
        </p>
      </header>
      {children}
    </section>
  )
}

export function Footer({
  hint,
  onBack,
  children,
}: {
  hint: string
  onBack?: () => void
  children?: React.ReactNode
}) {
  return (
    <footer
      style={{
        padding: '14px 24px',
        borderTop: '1px solid var(--border)',
        background: 'var(--surface)',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}
    >
      {onBack && (
        <Button size="sm" variant="ghost" onClick={onBack}>
          <ArrowLeft size={13} style={{ marginRight: 4 }} /> Back
        </Button>
      )}
      <span style={{ fontSize: 11, color: 'var(--text-faint)', flex: 1 }}>{hint}</span>
      {children}
    </footer>
  )
}

export function NextLabel({ children }: { children: React.ReactNode }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center' }}>
      {children}
      <ArrowRight size={13} style={{ marginLeft: 5 }} />
    </span>
  )
}

export function Field({
  label,
  optional,
  help,
  examples,
  children,
}: {
  label: string
  optional?: boolean
  help?: string
  examples?: string[]
  children: React.ReactNode
}) {
  return (
    <div style={{ paddingBottom: 18 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 5 }}>
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            color: 'var(--text)',
            textTransform: 'uppercase',
            letterSpacing: 0.5,
          }}
        >
          {label}
        </span>
        {optional && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              color: 'var(--text-faint)',
              textTransform: 'uppercase',
              letterSpacing: 0.4,
            }}
          >
            optional
          </span>
        )}
      </div>
      {help && (
        <p style={{ margin: '0 0 7px', fontSize: 11.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>
          {help}
        </p>
      )}
      {children}
      {examples && examples.length > 0 && (
        <ul
          style={{
            margin: '6px 0 0',
            paddingLeft: 16,
            fontSize: 11,
            color: 'var(--text-faint)',
            fontFamily: 'var(--font-mono)',
            lineHeight: 1.7,
          }}
        >
          {examples.map((example) => (
            <li key={example}>{example}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function SecretInput({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  placeholder: string
}) {
  return (
    <div style={{ position: 'relative' }}>
      <KeyRound
        size={13}
        style={{ position: 'absolute', left: 11, top: 11, color: 'var(--text-faint)' }}
      />
      <input
        type="password"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete="off"
        spellCheck={false}
        style={{ ...monoInputStyle, paddingLeft: 32 }}
      />
    </div>
  )
}

export function ChoiceButton({
  active,
  onClick,
  icon,
  label,
  hint,
}: {
  active: boolean
  onClick: () => void
  icon?: React.ReactNode
  label: string
  hint: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      style={{
        textAlign: 'left',
        padding: '9px 11px',
        borderRadius: 8,
        border: `1px solid ${active ? 'var(--text)' : 'var(--border)'}`,
        background: active ? 'var(--content-bg)' : 'var(--surface)',
        cursor: 'pointer',
        fontFamily: 'inherit',
        boxShadow: active ? '0 0 0 1px var(--text) inset' : 'none',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          fontSize: 12,
          fontWeight: 600,
          color: 'var(--text)',
        }}
      >
        {icon}
        {label}
      </div>
      <div
        style={{
          fontSize: 10.5,
          color: 'var(--text-dim)',
          marginTop: 2,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {hint}
      </div>
    </button>
  )
}

export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: 'var(--text-faint)',
          textTransform: 'uppercase',
          letterSpacing: 0.4,
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text)', marginTop: 2 }}>
        {value}
      </div>
    </div>
  )
}

export function WarningList({
  warnings,
  inline,
}: {
  warnings: OpenApiWarning[]
  inline?: boolean
}) {
  return (
    <div style={{ padding: inline ? '0 0 8px' : '0 24px 14px' }}>
      <div
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: 'var(--text-faint)',
          textTransform: 'uppercase',
          letterSpacing: 0.4,
          marginBottom: 5,
        }}
      >
        {warnings.length} warning{warnings.length === 1 ? '' : 's'} — the import still succeeded
      </div>
      {warnings.slice(0, 8).map((warning, index) => (
        <div key={index} style={{ fontSize: 11.5, color: 'var(--text-dim)', padding: '2px 0' }}>
          <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-faint)' }}>
            {warning.code}
          </span>{' '}
          {warning.message}
          {warning.context ? ` (${warning.context})` : ''}
        </div>
      ))}
    </div>
  )
}

import { useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { SutrButton } from './Button'

/** Copy-to-clipboard that reports what actually happened. */
export function SutrCopyButton({
  value,
  label = 'Copy',
  size = 'sm',
}: {
  value: string
  label?: string
  size?: 'sm' | 'md'
}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  async function copy() {
    try {
      await navigator.clipboard.writeText(value)
      setState('copied')
    } catch {
      setState('failed')
    }
    setTimeout(() => setState('idle'), 1600)
  }

  return (
    <SutrButton
      variant="ghost"
      size={size}
      iconOnly
      onClick={copy}
      aria-label={state === 'failed' ? 'Copy failed — select the text manually' : label}
      title={state === 'failed' ? 'Copy failed — select the text manually' : label}
    >
      {state === 'copied' ? (
        <Check size={13} style={{ color: 'var(--green)' }} />
      ) : (
        <Copy size={13} style={{ color: state === 'failed' ? 'var(--red)' : undefined }} />
      )}
    </SutrButton>
  )
}

export function SutrCodeBlock({
  code,
  label,
  copyable = true,
  maxHeight,
  actions,
}: {
  code: string
  label?: string
  copyable?: boolean
  maxHeight?: number
  actions?: ReactNode
}) {
  return (
    <div className="sutr-code">
      {(label || copyable || actions) && (
        <div className="sutr-code__bar">
          <span className="sutr-code__label">{label}</span>
          <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 2 }}>
            {actions}
            {copyable ? <SutrCopyButton value={code} /> : null}
          </span>
        </div>
      )}
      <pre
        className="sutr-code__pre"
        style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}
      >
        {code}
      </pre>
    </div>
  )
}

export function SutrInlineCode({ children }: { children: ReactNode }) {
  return <code className="sutr-code--inline">{children}</code>
}

export function SutrKbd({ children }: { children: ReactNode }) {
  return <kbd className="sutr-kbd">{children}</kbd>
}

/** A URL the operator is meant to take away — always copyable, never truncated
 *  out of reach. */
export function SutrEndpoint({ url, label }: { url: string; label?: string }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      {label ? <span className="sutr-section-label">{label}</span> : null}
      <div className="sutr-endpoint">
        <span className="sutr-endpoint__value" title={url}>
          {url}
        </span>
        <SutrCopyButton value={url} label="Copy endpoint" />
      </div>
    </div>
  )
}

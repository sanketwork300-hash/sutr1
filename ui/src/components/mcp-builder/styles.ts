/**
 * The builder's shared style vocabulary.
 *
 * Separate from `primitives.tsx` because a module that exports both
 * components and plain values breaks fast refresh; keeping the values here
 * means editing a colour does not remount the wizard.
 */

export const spin: React.CSSProperties = { animation: 'spin 1s linear infinite' }

export function bannerStyle(tone: 'error' | 'info' | 'success'): React.CSSProperties {
  const palette = {
    error: ['var(--badge-red-bg)', 'var(--badge-red-text)'],
    info: ['var(--badge-blue-bg)', 'var(--badge-blue-text)'],
    success: ['var(--badge-green-bg)', 'var(--badge-green-text)'],
  }[tone]
  return {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    margin: '0 0 14px',
    padding: '9px 12px',
    borderRadius: 8,
    fontSize: 12.5,
    lineHeight: 1.5,
    background: palette[0],
    color: palette[1],
  }
}

export const backLinkStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  border: 'none',
  background: 'transparent',
  color: 'var(--text-dim)',
  fontSize: 12,
  cursor: 'pointer',
  padding: '0 0 14px',
  fontFamily: 'inherit',
}

export const linkButtonStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  border: 'none',
  background: 'transparent',
  color: 'var(--text-dim)',
  fontSize: 11.5,
  fontFamily: 'inherit',
  cursor: 'pointer',
  padding: 0,
  textDecoration: 'underline',
}

export const inputStyle: React.CSSProperties = {
  width: '100%',
  height: 36,
  border: '1px solid var(--border)',
  borderRadius: 7,
  background: 'var(--input-bg)',
  color: 'var(--text)',
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
  padding: '0 11px',
  minWidth: 0,
}

export const monoInputStyle: React.CSSProperties = {
  ...inputStyle,
  fontFamily: 'var(--font-mono)',
  fontSize: 12.5,
}

export const subLabelStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  color: 'var(--text-faint)',
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  marginBottom: 5,
}

export const radioRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: 13,
  color: 'var(--text)',
  cursor: 'pointer',
}

export const authPreviewStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '8px 12px',
  borderRadius: 6,
  background: 'var(--code-bg)',
  color: 'var(--code-text)',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  overflow: 'hidden',
}

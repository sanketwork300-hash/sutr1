/**
 * The Swaraj Sutr mark: a routing node with four bound paths — the connective
 * layer, drawn as infrastructure rather than as an emblem. No flags, no
 * chakras, no monuments; the Indian identity of the product lives in what it
 * stands for, not in decoration.
 */
export function BrandMark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      style={{ flexShrink: 0 }}
    >
      <path
        d="M12 2.6 21.4 12 12 21.4 2.6 12 12 2.6Z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        opacity="0.45"
      />
      <path d="M12 7.2v9.6M7.2 12h9.6" stroke="currentColor" strokeWidth="1.1" opacity="0.3" />
      <circle cx="12" cy="12" r="3.1" fill="var(--brand)" />
      <circle cx="12" cy="4.4" r="1.35" fill="currentColor" />
      <circle cx="19.6" cy="12" r="1.35" fill="currentColor" />
      <circle cx="12" cy="19.6" r="1.35" fill="currentColor" />
      <circle cx="4.4" cy="12" r="1.35" fill="currentColor" />
    </svg>
  )
}

export function BrandLockup({
  size = 20,
  showParent = true,
}: {
  size?: number
  showParent?: boolean
}) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: 'var(--text)' }}>
      <BrandMark size={size} />
      <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
        {showParent ? (
          <span
            style={{
              fontSize: 8.5,
              fontWeight: 600,
              letterSpacing: '0.22em',
              color: 'var(--text-faint)',
              textTransform: 'uppercase',
              marginBottom: 2,
            }}
          >
            Swaraj
          </span>
        ) : null}
        <span
          style={{
            fontSize: size * 0.78,
            fontWeight: 600,
            letterSpacing: '-0.02em',
            color: 'var(--text)',
          }}
        >
          Sutr
        </span>
      </span>
    </span>
  )
}

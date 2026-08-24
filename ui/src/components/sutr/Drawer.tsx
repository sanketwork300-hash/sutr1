import { useId, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { useDismissable } from './useDismissable'
import { SutrButton } from './Button'

interface DrawerProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  footer?: ReactNode
  wide?: boolean
  children: ReactNode
}

/**
 * The inspector surface. Anything that details a single object — a tool, an
 * execution, a deployment — opens here rather than navigating away, so the
 * list the operator was scanning stays on screen behind it.
 */
export function SutrDrawer({
  open,
  onClose,
  title,
  subtitle,
  actions,
  footer,
  wide,
  children,
}: DrawerProps) {
  const titleId = useId()
  const ref = useDismissable(open, onClose)

  if (!open) return null

  return (
    <div
      className="sutr-overlay"
      style={{ padding: 0, alignItems: 'stretch', justifyContent: 'flex-end' }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`sutr-drawer${wide ? ' sutr-drawer--wide' : ''}`}
      >
        <div className="sutr-modal__header">
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
            <h2 id={titleId} className="sutr-h3" style={{ wordBreak: 'break-word' }}>
              {title}
            </h2>
            {subtitle ? <span className="sutr-meta">{subtitle}</span> : null}
          </div>
          {actions}
          <SutrButton variant="ghost" size="sm" iconOnly onClick={onClose} aria-label="Close">
            <X size={14} />
          </SutrButton>
        </div>
        <div className="sutr-modal__body">{children}</div>
        {footer ? <div className="sutr-modal__footer">{footer}</div> : null}
      </div>
    </div>
  )
}

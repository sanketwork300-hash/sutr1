import { useId, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { useDismissable } from './useDismissable'
import { SutrButton } from './Button'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  /** Footer is where decisions live; body is where information lives. */
  footer?: ReactNode
  wide?: boolean
  children: ReactNode
}

export function SutrModal({
  open,
  onClose,
  title,
  description,
  footer,
  wide,
  children,
}: ModalProps) {
  const titleId = useId()
  const descId = useId()
  const ref = useDismissable(open, onClose)

  if (!open) return null

  return (
    <div
      className="sutr-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={`sutr-modal${wide ? ' sutr-modal--wide' : ''}`}
      >
        <div className="sutr-modal__header">
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <h2 id={titleId} className="sutr-h3">
              {title}
            </h2>
            {description ? (
              <p id={descId} className="sutr-body" style={{ fontSize: 12.5 }}>
                {description}
              </p>
            ) : null}
          </div>
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

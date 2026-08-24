import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { Search } from 'lucide-react'

export function SutrField({
  label,
  hint,
  error,
  required,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  required?: boolean
  /** Receives the generated id so the control is properly labelled. */
  children: (props: { id: string; 'aria-describedby'?: string }) => ReactNode
}) {
  const id = useId()
  const descId = `${id}-desc`
  return (
    <div className="sutr-field">
      <label className="sutr-field__label" htmlFor={id}>
        {label}
        {required ? <span style={{ color: 'var(--brand)' }}>*</span> : null}
      </label>
      {children({ id, 'aria-describedby': hint || error ? descId : undefined })}
      {error ? (
        <span id={descId} className="sutr-field__error">
          {error}
        </span>
      ) : hint ? (
        <span id={descId} className="sutr-field__hint">
          {hint}
        </span>
      ) : null}
    </div>
  )
}

export function SutrInput({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={['sutr-input', className ?? ''].filter(Boolean).join(' ')} {...rest} />
}

export function SutrTextarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea className={['sutr-input', className ?? ''].filter(Boolean).join(' ')} {...rest} />
  )
}

export function SutrSelect({
  className,
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={['sutr-input', className ?? ''].filter(Boolean).join(' ')} {...rest}>
      {children}
    </select>
  )
}

export function SutrSearchInput({
  value,
  onValueChange,
  placeholder = 'Search',
  ariaLabel,
  maxWidth,
}: {
  value: string
  onValueChange: (next: string) => void
  placeholder?: string
  ariaLabel: string
  maxWidth?: number
}) {
  return (
    <div className="sutr-search" style={maxWidth ? { maxWidth, flex: '1 1 auto' } : undefined}>
      <span className="sutr-search__icon">
        <Search size={13} />
      </span>
      <SutrInput
        type="search"
        value={value}
        aria-label={ariaLabel}
        placeholder={placeholder}
        onChange={(e) => onValueChange(e.target.value)}
      />
    </div>
  )
}

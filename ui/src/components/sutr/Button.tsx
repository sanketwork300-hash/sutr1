import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { Link } from 'react-router-dom'

export type ButtonVariant = 'brand' | 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-solid'

export type ButtonSize = 'sm' | 'md' | 'lg'

interface CommonProps {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Renders as a square icon button; `children` is then the icon alone. */
  iconOnly?: boolean
  block?: boolean
  loading?: boolean
  className?: string
  children?: ReactNode
}

function classes({ variant = 'secondary', size = 'md', iconOnly, block, className }: CommonProps) {
  return [
    'sutr-btn',
    `sutr-btn--${variant}`,
    size !== 'md' ? `sutr-btn--${size}` : '',
    iconOnly ? 'sutr-btn--icon' : '',
    block ? 'sutr-btn--block' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ')
}

type ButtonProps = CommonProps & ButtonHTMLAttributes<HTMLButtonElement>

export function SutrButton({
  variant,
  size,
  iconOnly,
  block,
  loading,
  className,
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={classes({ variant, size, iconOnly, block, className })}
      {...rest}
    >
      {loading && <span className="sutr-btn__spinner" aria-hidden="true" />}
      {children}
    </button>
  )
}

/** Same visual language, but navigates. Use for anything that changes route. */
export function SutrLinkButton({
  to,
  variant,
  size,
  iconOnly,
  block,
  className,
  children,
  ...rest
}: CommonProps & { to: string } & Omit<React.ComponentProps<typeof Link>, 'to' | 'className'>) {
  return (
    <Link to={to} className={classes({ variant, size, iconOnly, block, className })} {...rest}>
      {children}
    </Link>
  )
}

/** For destinations outside the app (docs, provider consoles, MCP endpoints). */
export function SutrExternalButton({
  href,
  variant,
  size,
  iconOnly,
  block,
  className,
  children,
  ...rest
}: CommonProps & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={classes({ variant, size, iconOnly, block, className })}
      {...rest}
    >
      {children}
    </a>
  )
}

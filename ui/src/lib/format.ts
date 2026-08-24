/** Formatting shared by every console surface, so a timestamp reads the same
 *  in Activity as it does in a deployment detail drawer. */

/** Server timestamps are UTC; some arrive without a zone designator. */
export function parseTimestamp(value: string): Date {
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`
  return new Date(normalized)
}

export function formatClock(value: string): string {
  const date = parseTimestamp(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function formatDateTime(value: string): string {
  const date = parseTimestamp(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function formatDate(value: string): string {
  const date = parseTimestamp(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(undefined, { month: 'short', day: '2-digit' })
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 31536000],
  ['month', 2592000],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
]

export function relativeTime(value: string): string {
  const date = parseTimestamp(value)
  if (Number.isNaN(date.getTime())) return value
  const seconds = (date.getTime() - Date.now()) / 1000
  const magnitude = Math.abs(seconds)
  if (magnitude < 45) return seconds < 0 ? 'just now' : 'in a moment'
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  for (const [unit, size] of UNITS) {
    if (magnitude >= size || unit === 'second') {
      return formatter.format(Math.round(seconds / size), unit)
    }
  }
  return ''
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString()
}

/** Pretty-prints JSON that arrived as a string, and leaves it alone when it
 *  is not JSON at all — a malformed payload is still evidence. */
export function prettyJson(raw: string | null | undefined): string {
  if (!raw) return ''
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

/** The greeting on the overview: local morning/afternoon/evening. */
export function greeting(date = new Date()): string {
  const hour = date.getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

/** Best-effort display name from an email address, used only for greetings. */
export function displayName(email: string | null): string {
  if (!email) return 'there'
  const local = email.split('@')[0]
  const first = local.split(/[._-]/)[0]
  return first ? first.charAt(0).toUpperCase() + first.slice(1) : 'there'
}

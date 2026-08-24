import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CornerDownLeft, Search } from 'lucide-react'
import { useCommands, type Command } from './useCommands'
import { useCatalogStore } from '@/stores/catalog'
import { SutrKbd } from '@/components/sutr'
import { useDismissable } from '@/components/sutr/useDismissable'

/**
 * Cmd/Ctrl-K. Searches the workspace inventory and executes real navigation —
 * there is no decorative branch in here.
 */
export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const load = useCatalogStore((s) => s.load)
  const loading = useCatalogStore((s) => s.loading)
  const commands = useCommands(query)
  const containerRef = useDismissable(open, onClose)

  // The inventory is fetched the first time the palette is opened, not at boot.
  // Query and cursor need no reset here: AppShell remounts this component each
  // time it opens, so both start fresh.
  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const grouped = useMemo(() => {
    const groups = new Map<string, Command[]>()
    for (const command of commands) {
      const list = groups.get(command.group) ?? []
      list.push(command)
      groups.set(command.group, list)
    }
    return [...groups.entries()]
  }, [commands])

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>('[data-active="true"]')
    node?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  if (!open) return null

  function run(command: Command) {
    onClose()
    navigate(command.to)
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setCursor((c) => Math.min(c + 1, commands.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setCursor((c) => Math.max(c - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const command = commands[cursor]
      if (command) run(command)
    }
  }

  let index = -1

  return (
    <div
      className="palette__overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={containerRef}
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={onKeyDown}
      >
        <div className="palette__input-row">
          <Search size={15} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
          <input
            ref={inputRef}
            autoFocus
            className="palette__input"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setCursor(0)
            }}
            placeholder="Search tools, integrations, deployments, APIs…"
            aria-label="Search commands"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-results"
            aria-autocomplete="list"
          />
          {loading ? <span className="sutr-meta">Indexing…</span> : null}
          <SutrKbd>ESC</SutrKbd>
        </div>

        <div className="palette__list" id="palette-results" role="listbox" ref={listRef}>
          {commands.length === 0 ? (
            <div style={{ padding: '24px 12px', textAlign: 'center' }}>
              <p className="sutr-body">No match for “{query}”.</p>
            </div>
          ) : (
            grouped.map(([group, items]) => (
              <div key={group}>
                <div className="palette__group">{group}</div>
                {items.map((command) => {
                  index += 1
                  const active = index === cursor
                  const position = index
                  return (
                    <button
                      key={command.id}
                      type="button"
                      role="option"
                      aria-selected={active}
                      data-active={active}
                      className="palette__item"
                      onMouseEnter={() => setCursor(position)}
                      onClick={() => run(command)}
                    >
                      <span className="sutr-truncate">{command.label}</span>
                      {command.hint ? (
                        <span className="palette__item-hint">{command.hint}</span>
                      ) : null}
                      {active ? (
                        <CornerDownLeft
                          size={12}
                          style={{
                            color: 'var(--text-faint)',
                            marginLeft: command.hint ? 6 : 'auto',
                          }}
                        />
                      ) : null}
                    </button>
                  )
                })}
              </div>
            ))
          )}
        </div>

        <div className="palette__foot">
          <span>
            <SutrKbd>↑</SutrKbd> <SutrKbd>↓</SutrKbd> navigate
          </span>
          <span>
            <SutrKbd>↵</SutrKbd> open
          </span>
          <span style={{ marginLeft: 'auto' }}>{commands.length} results</span>
        </div>
      </div>
    </div>
  )
}

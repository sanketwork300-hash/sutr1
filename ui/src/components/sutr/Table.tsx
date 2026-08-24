import type { ReactNode } from 'react'

export interface Column<Row> {
  key: string
  header: ReactNode
  /** Right-aligns and tabular-figures the cell — for counts and durations. */
  numeric?: boolean
  width?: number | string
  render: (row: Row) => ReactNode
}

interface TableProps<Row> {
  columns: Column<Row>[]
  rows: Row[]
  rowKey: (row: Row) => string
  onRowClick?: (row: Row) => void
  /** Minimum width before the wrapper starts scrolling horizontally. */
  minWidth?: number
  empty?: ReactNode
  caption?: string
}

export function SutrTable<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  minWidth = 640,
  empty,
  caption,
}: TableProps<Row>) {
  if (rows.length === 0 && empty) return <>{empty}</>

  return (
    <div className="sutr-table-wrap">
      <table className="sutr-table" style={{ minWidth }}>
        {caption ? <caption className="sutr-sr-only">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                style={{
                  width: col.width,
                  textAlign: col.numeric ? 'right' : undefined,
                }}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            // A clickable row keeps its row semantics — putting role="button"
            // on a <tr> would break the table for a screen reader. It is
            // focusable and answers Enter/Space instead, and rows that open
            // something also carry a real control in their actions cell.
            <tr
              key={rowKey(row)}
              data-clickable={onRowClick ? 'true' : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={
                onRowClick
                  ? (e) => {
                      if (e.target !== e.currentTarget) return
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onRowClick(row)
                      }
                    }
                  : undefined
              }
            >
              {columns.map((col) => (
                <td key={col.key} className={col.numeric ? 'sutr-table__num' : undefined}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

import { useRef, useState } from 'react'
import { FileJson, Upload, X } from 'lucide-react'
import {
  Field,
} from '@/components/mcp-builder/primitives'

const ACCEPTED = '.json,.yaml,.yml,application/json,text/yaml,application/x-yaml'
// Larger than any hand-written specification and comfortably under the
// server's own cap, so the rejection happens here with a useful message
// rather than after a long upload.
const MAX_BYTES = 8 * 1024 * 1024

/**
 * The upload source: a file from this machine, read in the browser.
 *
 * Worth being precise about, because "upload" invites the assumption that the
 * file goes somewhere third-party: it does not. The file is read locally and
 * its text is sent to the sutr server in the same import request a pasted
 * document would use. The filename travels too, purely as provenance, so a
 * project imported this way still records where it came from.
 */
export function UploadSource({
  content,
  onContent,
  filename,
  onFilename,
}: {
  content: string
  onContent: (value: string) => void
  filename: string
  onFilename: (value: string) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')

  async function accept(file: File | undefined) {
    if (!file) return
    if (file.size > MAX_BYTES) {
      setError(
        `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB, over the 8 MB limit.`,
      )
      return
    }
    setError('')
    onContent(await file.text())
    onFilename(file.name)
  }

  return (
    <Field
      label="Specification file"
      help="JSON or YAML, OpenAPI 3.0 or 3.1. Swagger 2.0 is rejected — convert it first. The file is read in your browser and its contents are sent to this sutr server with the import; it is not uploaded anywhere else, and the file itself is never stored."
    >
      <div
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          void accept(event.dataTransfer.files?.[0])
        }}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click()
        }}
        style={{
          border: `1px dashed ${dragging ? 'var(--text)' : 'var(--border-strong, var(--border))'}`,
          borderRadius: 10,
          background: dragging ? 'var(--surface-hover)' : 'var(--surface)',
          padding: '22px 16px',
          textAlign: 'center',
          cursor: 'pointer',
          transition: 'background 120ms ease, border-color 120ms ease',
        }}
      >
        <Upload size={18} style={{ color: 'var(--text-dim)' }} />
        <div style={{ fontSize: 12.5, color: 'var(--text)', marginTop: 8 }}>
          Drop a specification here, or click to choose one
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 3 }}>
          openapi.yaml · openapi.json · swagger.yaml · up to 8 MB
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED}
          style={{ display: 'none' }}
          onChange={(event) => {
            void accept(event.target.files?.[0])
            // Let the same file be chosen again after an edit on disk.
            event.target.value = ''
          }}
        />
      </div>

      {error && (
        <p style={{ margin: '8px 0 0', fontSize: 11.5, color: 'var(--badge-red-text)' }}>{error}</p>
      )}

      {content && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginTop: 10,
            padding: '8px 11px',
            borderRadius: 8,
            border: '1px solid var(--border)',
            background: 'var(--content-bg)',
          }}
        >
          <FileJson size={14} style={{ color: 'var(--text-dim)', flexShrink: 0 }} />
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              color: 'var(--text)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {filename || 'specification'}
          </span>
          <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
            {(content.length / 1024).toFixed(1)} KB
          </span>
          <button
            type="button"
            aria-label="Remove file"
            onClick={(event) => {
              event.stopPropagation()
              onContent('')
              onFilename('')
            }}
            style={{
              border: 'none',
              background: 'transparent',
              color: 'var(--text-faint)',
              cursor: 'pointer',
              display: 'inline-flex',
              padding: 0,
            }}
          >
            <X size={14} />
          </button>
        </div>
      )}
    </Field>
  )
}

import { useReducedMotion } from '@/lib/useReducedMotion'

const NODE_FILL = 'var(--surface)'
const NODE_STROKE = 'var(--border-strong)'

function Node({
  x,
  y,
  w,
  h = 42,
  label,
  detail,
  accent,
}: {
  x: number
  y: number
  w: number
  h?: number
  label: string
  detail?: string
  accent?: boolean
}) {
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        rx={8}
        fill={accent ? 'var(--brand-soft)' : NODE_FILL}
        stroke={accent ? 'var(--brand-border)' : NODE_STROKE}
      />
      <text
        x={x + w / 2}
        y={detail ? y + h / 2 - 3 : y + h / 2 + 4}
        textAnchor="middle"
        fontSize="11"
        fontWeight="600"
        letterSpacing="1.1"
        fill={accent ? 'var(--brand)' : 'var(--text)'}
        fontFamily="var(--font-mono)"
      >
        {label}
      </text>
      {detail ? (
        <text
          x={x + w / 2}
          y={y + h / 2 + 12}
          textAnchor="middle"
          fontSize="9.5"
          fill="var(--text-faint)"
          fontFamily="var(--font-mono)"
          letterSpacing="0.6"
        >
          {detail}
        </text>
      ) : null}
    </g>
  )
}

function Chip({ x, y, label }: { x: number; y: number; label: string }) {
  const w = label.length * 6.2 + 18
  return (
    <g>
      <rect
        x={x}
        y={y}
        width={w}
        height={20}
        rx={5}
        fill="var(--content-bg)"
        stroke="var(--border)"
      />
      <text
        x={x + w / 2}
        y={y + 13.5}
        textAnchor="middle"
        fontSize="9"
        letterSpacing="0.9"
        fill="var(--text-dim)"
        fontFamily="var(--font-mono)"
      >
        {label}
      </text>
    </g>
  )
}

/**
 * The request path, drawn as infrastructure: an agent reaches the gateway, the
 * gateway authenticates it, applies policy and — when policy says so — waits
 * for a human, then routes to whichever kind of capability answers.
 *
 * Traffic is a handful of small circles moving along the same paths the lines
 * describe. When the viewer prefers reduced motion they are simply not
 * rendered: the topology is the message, the movement is the emphasis.
 */
export function HeroGraph() {
  const reduced = useReducedMotion()

  const flow = (delay: number, path: string, duration = 3.4) => (
    <circle r="2.6" fill="var(--brand)">
      <animateMotion
        dur={`${duration}s`}
        begin={`${delay}s`}
        repeatCount="indefinite"
        path={path}
      />
      <animate
        attributeName="opacity"
        values="0;1;1;0"
        keyTimes="0;0.12;0.85;1"
        dur={`${duration}s`}
        begin={`${delay}s`}
        repeatCount="indefinite"
      />
    </circle>
  )

  const trunk = 'M260,66 L260,104'
  const fanIn = 'M260,196 L260,216'
  const toApi = 'M260,216 L160,216 L160,238'
  const toMcp = 'M260,216 L260,238'
  const toApp = 'M260,216 L360,216 L360,238'
  const toProvider = 'M260,280 L260,318'

  return (
    <svg
      viewBox="0 0 520 400"
      width="100%"
      height="auto"
      role="img"
      aria-label="An agent request enters the Sutr gateway, is authenticated, checked against policy and — where required — approved by a person, then routed to an API tool, an MCP server or a native integration before reaching the upstream provider."
      style={{ display: 'block', maxHeight: 460 }}
    >
      <defs>
        <marker id="lp-arrow" markerWidth="7" markerHeight="7" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 z" fill="var(--border-strong)" />
        </marker>
      </defs>

      {/* Agent */}
      <Node x={180} y={24} w={160} label="AI AGENT" detail="MCP client · SDK · CLI" />

      {/* Agent → gateway */}
      <path
        d={trunk}
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        markerEnd="url(#lp-arrow)"
        fill="none"
      />

      {/* Gateway */}
      <rect
        x={110}
        y={108}
        width={300}
        height={88}
        rx={10}
        fill="var(--brand-soft)"
        stroke="var(--brand-border)"
      />
      <text
        x={260}
        y={130}
        textAnchor="middle"
        fontSize="12"
        fontWeight="600"
        letterSpacing="1.4"
        fill="var(--brand)"
        fontFamily="var(--font-mono)"
      >
        SUTR GATEWAY
      </text>
      <Chip x={126} y={148} label="IDENTITY" />
      <Chip x={206} y={148} label="POLICY" />
      <Chip x={274} y={148} label="APPROVAL" />
      <Chip x={358} y={148} label="AUDIT" />

      {/* Gateway → capabilities */}
      <path d={fanIn} stroke={NODE_STROKE} strokeWidth="1.2" fill="none" />
      <path
        d={toApi}
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        markerEnd="url(#lp-arrow)"
        fill="none"
      />
      <path
        d={toMcp}
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        markerEnd="url(#lp-arrow)"
        fill="none"
      />
      <path
        d={toApp}
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        markerEnd="url(#lp-arrow)"
        fill="none"
      />

      <Node x={112} y={238} w={96} label="API TOOL" detail="from OpenAPI" />
      <Node x={212} y={238} w={96} label="MCP SERVER" detail="remote" accent />
      <Node x={312} y={238} w={96} label="INTEGRATION" detail="native" />

      {/* Capabilities → provider */}
      <path
        d="M160,280 L160,298 L360,298 L360,280"
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        fill="none"
      />
      <path
        d={toProvider}
        stroke={NODE_STROKE}
        strokeWidth="1.2"
        markerEnd="url(#lp-arrow)"
        fill="none"
      />

      <Node
        x={170}
        y={322}
        w={180}
        label="UPSTREAM PROVIDER"
        detail="credential injected server-side"
      />

      {!reduced ? (
        <>
          {flow(0, trunk, 2.2)}
          {flow(0.9, toMcp, 2.2)}
          {flow(1.7, toApi, 2.4)}
          {flow(2.4, toApp, 2.4)}
          {flow(1.4, toProvider, 2)}
        </>
      ) : null}
    </svg>
  )
}

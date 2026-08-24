import { useState } from 'react'
import { SutrCodeBlock } from '@/components/sutr'

/**
 * Every snippet below is taken from the shipped SDKs and CLI, not written for
 * the page. `sutr tools call` really does require --integration and --tool;
 * the SDK really does raise ApprovalRequired.
 */
const SURFACES = [
  {
    id: 'mcp',
    label: 'MCP',
    language: 'json',
    code: `{
  "mcpServers": {
    "sutr": {
      "type": "http",
      "url": "https://your-sutr-instance/mcp",
      "headers": { "Authorization": "Bearer ap_..." }
    }
  }
}`,
  },
  {
    id: 'python',
    label: 'Python SDK',
    language: 'python',
    code: `from sutr_sdk import Sutr

with Sutr(api_key="ap_...", base_url="https://sutr.example.com") as sutr:
    for tool in sutr.list_tools():
        print(tool.integration_id, tool.name, tool.execution_mode)

    result = sutr.call_tool(
        "stripe",
        "create_refund",
        {"charge": "ch_123"},
        additional_info="Customer reported a duplicate charge",
        wait_for_approval=True,
    )
    print(result.text)`,
  },
  {
    id: 'typescript',
    label: 'TypeScript SDK',
    language: 'typescript',
    code: `import { Sutr } from '@sutr/sdk';

const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });

for (const tool of await sutr.listTools()) {
  console.log(tool.integrationId, tool.name, tool.executionMode);
}

const result = await sutr.callTool('posthog', 'create_annotation', {
  content: 'shipped',
});`,
  },
  {
    id: 'cli',
    label: 'CLI',
    language: 'bash',
    code: `sutr auth login --api-key ap_...
sutr tools list --integration stripe
sutr tools call --integration stripe --tool create_refund \\
  --args '{"charge":"ch_123"}' --wait
sutr deploy create --provider gcp
sutr usage --days 30`,
  },
  {
    id: 'rest',
    label: 'REST',
    language: 'bash',
    code: `curl -X POST https://sutr.example.com/api/tools/stripe/call \\
  -H "Authorization: Bearer ap_..." \\
  -H "Content-Type: application/json" \\
  -d '{"tool_name":"create_refund","args":{"charge":"ch_123"}}'`,
  },
]

export function DeveloperSection() {
  const [active, setActive] = useState(SURFACES[0].id)
  const surface = SURFACES.find((s) => s.id === active) ?? SURFACES[0]

  return (
    <section className="lp__section" id="developers">
      <div className="lp__shell">
        <div className="lp__section-head">
          <span className="sutr-eyebrow sutr-eyebrow--brand">Developers</span>
          <h2 className="sutr-h1">One platform. Every interface.</h2>
          <p className="sutr-lead">
            The same governed execution path whether the caller is an MCP client, an SDK, the CLI or
            plain HTTP.
          </p>
        </div>

        <div className="lp__stages" role="tablist" aria-label="Developer interfaces">
          {SURFACES.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={item.id === active}
              className="lp__stage"
              style={{ flex: '0 1 auto' }}
              onClick={() => setActive(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <SutrCodeBlock code={surface.code} label={surface.language} maxHeight={340} />
      </div>
    </section>
  )
}

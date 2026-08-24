import { useState } from 'react'
import {
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrCodeBlock,
  SutrLinkButton,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrTabs,
} from '@/components/sutr'

type Language = 'python' | 'typescript'

/**
 * The shipped SDKs, documented from their own READMEs. Nothing here describes
 * an API the packages do not expose.
 */
const SDKS = {
  python: {
    label: 'Python',
    install: 'pip install sutr-sdk',
    quickStart: `from sutr_sdk import Sutr

with Sutr(api_key="ap_...", base_url="https://sutr.example.com") as sutr:
    for tool in sutr.list_tools():
        print(tool.integration_id, tool.name, tool.execution_mode)

    result = sutr.call_tool("posthog", "create_annotation", {"content": "shipped"})
    print(result.text)`,
    approval: `from sutr_sdk import ApprovalRequired, ToolDenied

try:
    result = sutr.call_tool("stripe", "create_refund", {"charge": "ch_123"})
except ApprovalRequired as gate:
    print(f"A human needs to approve this: {gate.approval_url}")
    decision = sutr.await_approval(gate.approval_request_id, timeout=600)
    if decision.approved:
        result = sutr.call_tool("stripe", "create_refund", {"charge": "ch_123"})
except ToolDenied:
    print("Policy blocks this tool outright — waiting will not help.")`,
    oneCall: `result = sutr.call_tool(
    "stripe",
    "create_refund",
    {"charge": "ch_123"},
    additional_info="Customer reported a duplicate charge (ticket 4821)",
    wait_for_approval=True,   # blocks until a human decides
    approval_timeout=600,     # raises ApprovalPending if nobody decides
)`,
    env: 'SUTR_API_KEY and SUTR_BASE_URL are used when the constructor arguments are omitted.',
    note: 'The import package is sutr_sdk and the distribution is sutr-sdk — both differ from the Sutr server package (sutr) on purpose, so the two can be installed side by side.',
  },
  typescript: {
    label: 'TypeScript',
    install: 'npm install @sutr/sdk',
    quickStart: `import { Sutr } from '@sutr/sdk';

const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });

for (const tool of await sutr.listTools()) {
  console.log(tool.integrationId, tool.name, tool.executionMode);
}

const result = await sutr.callTool('posthog', 'create_annotation', {
  content: 'shipped',
});
console.log(result.text);`,
    approval: `import { ApprovalRequired, ToolDenied } from '@sutr/sdk';

try {
  const result = await sutr.callTool('stripe', 'create_refund', { charge: 'ch_123' });
} catch (error) {
  if (error instanceof ApprovalRequired) {
    console.log(\`A human needs to approve this: \${error.approvalUrl}\`);
    const decision = await sutr.awaitApproval(error.approvalRequestId, {
      timeoutMs: 600_000,
    });
    if (decision.approved) {
      /* re-issue the identical call */
    }
  } else if (error instanceof ToolDenied) {
    console.log('Policy blocks this tool outright.');
  }
}`,
    oneCall: `const result = await sutr.callTool(
  'stripe',
  'create_refund',
  { charge: 'ch_123' },
  {
    additionalInfo: 'Customer reported a duplicate charge (ticket 4821)',
    waitForApproval: true,
  },
);`,
    env: 'apiKey and baseUrl fall back to SUTR_API_KEY and SUTR_BASE_URL.',
    note: 'Zero runtime dependencies; uses the platform fetch (Node 18+, Deno, Bun). A browser should never hold a Sutr API key.',
  },
} as const

export default function SdkPage() {
  const [language, setLanguage] = useState<Language>('python')
  const sdk = SDKS[language]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Develop"
        title="SDKs"
        subtitle="Call governed tools from your own code. Approval is a typed outcome, not an error you have to parse."
        actions={
          <SutrLinkButton to="/app/api-keys" variant="secondary" size="sm">
            Create an API key
          </SutrLinkButton>
        }
      >
        <SutrTabs
          ariaLabel="SDK language"
          value={language}
          onChange={setLanguage}
          items={[
            { value: 'python', label: 'Python' },
            { value: 'typescript', label: 'TypeScript' },
          ]}
        />
      </SutrPageHeader>

      <SutrPageBody>
        <SutrCard>
          <SutrCardHeader title="Install" meta={sdk.note} />
          <SutrCardBody>
            <SutrCodeBlock code={sdk.install} label="shell" />
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader title="Quick start" meta={sdk.env} />
          <SutrCardBody>
            <SutrCodeBlock code={sdk.quickStart} label={sdk.label.toLowerCase()} />
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="The approval flow"
            meta="A tool set to require approval does not execute until a person says so"
          />
          <SutrCardBody style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <SutrCodeBlock code={sdk.approval} label={sdk.label.toLowerCase()} />
            <p className="sutr-body">Or let the SDK wait for the decision in a single call:</p>
            <SutrCodeBlock code={sdk.oneCall} label={sdk.label.toLowerCase()} />
            <p className="sutr-meta">
              The approval is bound to the exact arguments that were shown to the approver. Re-issue
              the identical call after the decision — a changed argument needs its own approval.
            </p>
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>
    </SutrPage>
  )
}

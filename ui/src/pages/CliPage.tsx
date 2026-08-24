import {
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrCodeBlock,
  SutrLinkButton,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
} from '@/components/sutr'

/**
 * The CLI surface, transcribed from `cli/src/commands/`. Every command and
 * flag on this page exists in the shipped binary — including the required
 * `--integration` and `--tool` on `tools call`, which is the flag people most
 * often expect to be positional.
 */
const GROUPS = [
  {
    name: 'auth',
    summary: 'Point the CLI at an instance and authenticate with an API key.',
    commands: [
      { cmd: 'sutr auth set-instance-url <url>', note: 'Target a different Sutr instance' },
      { cmd: 'sutr auth login --api-key ap_...', note: 'Store credentials for this machine' },
      { cmd: 'sutr auth status', note: 'Show who the CLI is authenticated as' },
      { cmd: 'sutr auth logout', note: 'Remove stored credentials' },
    ],
  },
  {
    name: 'tools',
    summary: 'List, inspect and call governed tools.',
    commands: [
      { cmd: 'sutr tools list --integration stripe', note: 'Tools, with each one’s policy' },
      {
        cmd: 'sutr tools describe --integration stripe --tool create_refund',
        note: 'Parameters and metadata',
      },
      {
        cmd: 'sutr tools call --integration stripe --tool create_refund --args \'{"charge":"ch_123"}\'',
        note: 'Call a tool (alias: sutr tools run)',
      },
      {
        cmd: 'sutr tools call ... --wait --info "duplicate charge, ticket 4821"',
        note: 'Wait for approval and retry automatically',
      },
      { cmd: 'sutr tools await-approval --request-id req_...', note: 'Block on a decision' },
    ],
  },
  {
    name: 'openapi',
    summary: 'Import specifications and compile them into integrations.',
    commands: [
      { cmd: 'sutr openapi discover --url https://github.com/org/repo', note: 'Find spec files' },
      { cmd: 'sutr openapi import --file ./openapi.yaml', note: 'Import a document' },
      { cmd: 'sutr openapi list', note: 'Imported projects' },
      { cmd: 'sutr openapi show <project_id>', note: 'Operations, servers, suggested auth' },
      { cmd: 'sutr openapi compile <project_id> --dry-run', note: 'Preview the generated tools' },
    ],
  },
  {
    name: 'deploy',
    summary: 'Run a compiled project as a standalone MCP server.',
    commands: [
      { cmd: 'sutr deploy providers', note: 'Targets and whether each is configured' },
      { cmd: 'sutr deploy create --provider gcp', note: 'Deploy an OpenAPI project' },
      { cmd: 'sutr deploy list', note: 'Every deployment and its status' },
      { cmd: 'sutr deploy status <deployment_id>', note: 'Live status from the provider' },
      { cmd: 'sutr deploy logs <deployment_id> --tail 200', note: 'Container logs' },
      { cmd: 'sutr deploy delete <deployment_id>', note: 'Tear it down' },
    ],
  },
  {
    name: 'connections',
    summary: 'Authorize the accounts Sutr uses on your behalf.',
    commands: [
      { cmd: 'sutr connections list', note: 'Providers, setup state, who is connected' },
      { cmd: 'sutr connections connect github', note: 'Authorize github, gcp, azure or aws' },
      {
        cmd: 'sutr connections targets <connection_id>',
        note: 'Projects, subscriptions, accounts',
      },
      { cmd: 'sutr connections disconnect <connection_id>', note: 'Delete the stored tokens' },
    ],
  },
  {
    name: 'integrations · usage · output',
    summary: 'Install integrations, read the meter, choose an output format.',
    commands: [
      { cmd: 'sutr integrations list --installed', note: 'What this organisation has installed' },
      { cmd: 'sutr integrations add posthog --token ...', note: 'Install an integration' },
      { cmd: 'sutr usage --days 30', note: 'Totals and breakdowns' },
      { cmd: 'sutr usage events --kind tool_call --limit 50', note: 'Raw metered events' },
      { cmd: 'sutr output json', note: 'Set the default output format' },
    ],
  },
]

export default function CliPage() {
  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Develop"
        title="CLI"
        subtitle="The same governed execution path as the console and the SDKs, from a terminal."
        actions={
          <SutrLinkButton to="/app/api-keys" variant="secondary" size="sm">
            Create an API key
          </SutrLinkButton>
        }
      />

      <SutrPageBody>
        <SutrCard>
          <SutrCardHeader title="Install and authenticate" meta="Node 18 or newer" />
          <SutrCardBody>
            <SutrCodeBlock
              label="shell"
              code={`npm install -g sutr-cli

sutr auth set-instance-url ${typeof window === 'undefined' ? 'https://sutr.example.com' : window.location.origin}
sutr auth login --api-key ap_...
sutr auth status`}
            />
            <p className="sutr-meta" style={{ marginTop: 10 }}>
              Every command accepts <code className="sutr-code--inline">-o json</code> for
              scripting.
            </p>
          </SutrCardBody>
        </SutrCard>

        {GROUPS.map((group) => (
          <SutrCard key={group.name}>
            <SutrCardHeader title={group.name} meta={group.summary} />
            <SutrCardBody style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {group.commands.map((command) => (
                <div
                  key={command.cmd}
                  style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}
                >
                  <code
                    className="sutr-mono"
                    style={{
                      color: 'var(--text)',
                      background: 'var(--code-bg)',
                      border: '1px solid var(--border)',
                      borderRadius: 'var(--r-xs)',
                      padding: '7px 9px',
                      overflowX: 'auto',
                      whiteSpace: 'pre',
                    }}
                  >
                    {command.cmd}
                  </code>
                  <span className="sutr-meta">{command.note}</span>
                </div>
              ))}
            </SutrCardBody>
          </SutrCard>
        ))}
      </SutrPageBody>
    </SutrPage>
  )
}

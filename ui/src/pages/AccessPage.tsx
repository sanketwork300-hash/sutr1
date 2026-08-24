import { MembersSection } from '@/components/settings/MembersSection'
import { WorkspacesPanel } from '@/components/settings/WorkspacesPanel'
import {
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
} from '@/components/sutr'

/**
 * Who can reach this organisation and how its work is grouped. Both panels are
 * the ones the settings page has always used — the same endpoints, the same
 * server-side permission checks, presented as a governance surface rather than
 * as a settings tab.
 */
export default function AccessPage() {
  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Govern"
        title="Access"
        subtitle="Members, roles and invitations for this organisation, and the workspaces its infrastructure is grouped into."
      />

      <SutrPageBody>
        <SutrCard>
          <SutrCardHeader
            title="Organisation and members"
            meta="Roles are enforced by the server on every request, not just hidden in the console"
          />
          <SutrCardBody>
            <MembersSection />
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="Workspaces"
            meta="Named groupings for integrations and API projects; every organisation has a default"
          />
          <SutrCardBody>
            <WorkspacesPanel />
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>
    </SutrPage>
  )
}

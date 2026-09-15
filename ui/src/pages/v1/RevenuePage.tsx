import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrInput,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  SutrTabs,
  type Column,
} from '@/components/sutr'
import {
  formatBps,
  formatMicros,
  formatWhen,
  v1,
  type Invoice,
  type LedgerEntry,
  type PricingPlan,
  type Settlement,
} from '@/api/v1'
import { useAction, useResource } from './useResource'

type Tab = 'invoices' | 'plans' | 'ledger' | 'settlements'

const INVOICE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'neutral'> = {
  draft: 'neutral',
  issued: 'info',
  paid: 'success',
  unpaid: 'warning',
  void: 'neutral',
  failed: 'danger',
}

function firstOfMonth(offset = 0): string {
  const now = new Date()
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + offset, 1)).toISOString()
}

/**
 * Metering, invoices, the ledger and settlement (LLD §5.1).
 *
 * Two things this page will not do, because the platform does not: collect a
 * payment, or pay a provider out. Every invoice and settlement carries a field
 * saying so, and it is shown rather than hidden — an operator who thinks the
 * console charged a card would be wrong in an expensive direction.
 */
export default function RevenuePage() {
  const [tab, setTab] = useState<Tab>('invoices')
  const [openInvoice, setOpenInvoice] = useState<string | null>(null)
  const [periodStart, setPeriodStart] = useState(firstOfMonth(-1).slice(0, 10))
  const [periodEnd, setPeriodEnd] = useState(firstOfMonth(0).slice(0, 10))
  const action = useAction()

  const invoices = useResource(() => v1.billing.invoices(), [])
  const plans = useResource(() => v1.billing.plans(), [])
  const ledger = useResource(() => v1.billing.ledger(), [])
  const settlements = useResource(() => v1.settlements.list(), [])
  const capabilities = useResource(() => v1.metering.capabilities(), [])
  const invoice = useResource<Invoice | null>(
    () => (openInvoice ? v1.billing.invoice(openInvoice) : Promise.resolve(null)),
    [openInvoice],
  )

  async function generate() {
    const ok = await action.run(
      () =>
        v1.billing.generateInvoice({
          period_start: `${periodStart}T00:00:00Z`,
          period_end: `${periodEnd}T00:00:00Z`,
        }),
      'Invoice generated for that period.',
    )
    if (ok) invoices.reload()
  }

  async function issue(id: string) {
    const ok = await action.run(() => v1.billing.issueInvoice(id), 'Invoice issued.')
    if (ok) {
      invoices.reload()
      ledger.reload()
    }
  }

  async function publish(plan: PricingPlan) {
    const ok = await action.run(() => v1.billing.publishPlan(plan.id), `${plan.name} published.`)
    if (ok) plans.reload()
  }

  const invoiceColumns: Column<Invoice>[] = [
    { key: 'number', header: 'Invoice', render: (row) => <code>{row.number || row.id}</code> },
    {
      key: 'state',
      header: 'State',
      width: 110,
      render: (row) => (
        <SutrBadge tone={INVOICE_TONE[row.state] ?? 'neutral'} dot>
          {row.state}
        </SutrBadge>
      ),
    },
    {
      key: 'period',
      header: 'Period',
      width: 220,
      render: (row) => (
        <span className="sutr-muted">
          {formatWhen(row.period_start)} → {formatWhen(row.period_end)}
        </span>
      ),
    },
    {
      key: 'total',
      header: 'Total',
      numeric: true,
      width: 150,
      render: (row) => formatMicros(row.total_micros, row.currency),
    },
    {
      key: 'actions',
      header: '',
      width: 100,
      render: (row) =>
        row.state === 'draft' ? (
          <SutrButton
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              issue(row.id)
            }}
            disabled={action.busy}
          >
            Issue
          </SutrButton>
        ) : null,
    },
  ]

  const ledgerColumns: Column<LedgerEntry>[] = [
    { key: 'kind', header: 'Kind', render: (row) => <strong>{row.kind}</strong> },
    { key: 'direction', header: 'Direction', width: 110, render: (row) => row.direction ?? '—' },
    {
      key: 'amount',
      header: 'Amount',
      numeric: true,
      width: 150,
      render: (row) => formatMicros(row.amount_micros, row.currency),
    },
    {
      key: 'balance',
      header: 'Balance after',
      numeric: true,
      width: 150,
      render: (row) => formatMicros(row.balance_micros, row.currency),
    },
    { key: 'reason', header: 'Reason', render: (row) => row.reason ?? '' },
    { key: 'when', header: 'Recorded', width: 180, render: (row) => formatWhen(row.created_at) },
  ]

  const settlementColumns: Column<Settlement>[] = [
    { key: 'id', header: 'Settlement', render: (row) => <code>{row.id}</code> },
    {
      key: 'state',
      header: 'State',
      width: 120,
      render: (row) => (
        <SutrBadge
          tone={row.state === 'complete' ? 'success' : row.state === 'paused' ? 'warning' : 'info'}
          dot
        >
          {row.state}
        </SutrBadge>
      ),
    },
    {
      key: 'share',
      header: 'Provider share',
      width: 130,
      render: (row) => formatBps(row.provider_share_bps),
    },
    {
      key: 'provider',
      header: 'To provider',
      numeric: true,
      width: 150,
      render: (row) => formatMicros(row.provider_amount_micros),
    },
    {
      key: 'platform',
      header: 'To platform',
      numeric: true,
      width: 150,
      render: (row) => formatMicros(row.platform_amount_micros),
    },
    {
      key: 'paid',
      header: 'Paid out',
      width: 120,
      // Always false. The field exists so nobody has to assume.
      render: (row) =>
        row.paid_out_by_this_platform ? (
          <SutrBadge tone="success">paid</SutrBadge>
        ) : (
          <span className="sutr-muted">not by this platform</span>
        ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Account"
        title="Revenue"
        subtitle="Usage priced at billing time, an append-only ledger, and the provider revenue share. Nothing here moves money."
      >
        <SutrTabs
          items={[
            { value: 'invoices', label: 'Invoices', count: invoices.data?.length },
            { value: 'plans', label: 'Plans', count: plans.data?.length },
            { value: 'ledger', label: 'Ledger', count: ledger.data?.entries.length },
            { value: 'settlements', label: 'Settlements', count: settlements.data?.length },
          ]}
          value={tab}
          onChange={setTab}
          ariaLabel="Revenue view"
        />
      </SutrPageHeader>

      <SutrPageBody>
        <SutrCard>
          <SutrCardBody className="sutr-muted">
            This platform computes money and does not move it. Invoices are generated and
            settlements calculated; collecting a payment and paying a provider out need a payment
            processor that is not wired. Every response says so in a field.
          </SutrCardBody>
        </SutrCard>

        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {tab === 'invoices' ? (
          <>
            <SutrCard>
              <SutrCardHeader title="Generate an invoice" meta="prices the usage in a period" />
              <SutrCardBody>
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
                  <label>
                    <div className="sutr-muted">Period start</div>
                    <SutrInput
                      type="date"
                      value={periodStart}
                      onChange={(event) => setPeriodStart(event.target.value)}
                    />
                  </label>
                  <label>
                    <div className="sutr-muted">Period end</div>
                    <SutrInput
                      type="date"
                      value={periodEnd}
                      onChange={(event) => setPeriodEnd(event.target.value)}
                    />
                  </label>
                  <SutrButton onClick={generate} disabled={action.busy}>
                    Generate
                  </SutrButton>
                </div>
              </SutrCardBody>
            </SutrCard>

            {invoices.error ? <SutrError what={invoices.error} /> : null}
            {invoices.loading ? <SutrSpinner /> : null}
            {invoices.data ? (
              invoices.data.length ? (
                <SutrTable
                  columns={invoiceColumns}
                  rows={invoices.data}
                  rowKey={(row) => row.id}
                  onRowClick={(row) => setOpenInvoice(row.id)}
                  minWidth={820}
                />
              ) : (
                <SutrEmpty
                  title="No invoices"
                  body="Generating one prices the usage already recorded in that period. It does not create usage."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'plans' ? (
          <>
            {plans.error ? <SutrError what={plans.error} /> : null}
            {plans.data ? (
              plans.data.length ? (
                <SutrTable
                  columns={[
                    {
                      key: 'name',
                      header: 'Plan',
                      render: (row) => (
                        <>
                          <strong>{row.name || row.key}</strong>
                          <div className="sutr-muted">
                            {row.model} · v{row.version ?? 1}
                          </div>
                        </>
                      ),
                    },
                    {
                      key: 'state',
                      header: 'State',
                      width: 120,
                      render: (row) => (
                        <SutrBadge tone={row.state === 'published' ? 'success' : 'neutral'} dot>
                          {row.state ?? 'draft'}
                        </SutrBadge>
                      ),
                    },
                    {
                      key: 'amount',
                      header: 'Rate',
                      numeric: true,
                      width: 160,
                      render: (row) => formatMicros(row.amount_micros, row.currency),
                    },
                    {
                      key: 'included',
                      header: 'Included',
                      numeric: true,
                      width: 100,
                      render: (row) => row.included_units ?? 0,
                    },
                    {
                      key: 'actions',
                      header: '',
                      width: 110,
                      render: (row) =>
                        row.state === 'published' ? null : (
                          <SutrButton size="sm" onClick={() => publish(row)} disabled={action.busy}>
                            Publish
                          </SutrButton>
                        ),
                    },
                  ]}
                  rows={plans.data}
                  rowKey={(row) => row.id}
                  minWidth={760}
                />
              ) : (
                <SutrEmpty
                  title="No pricing plans"
                  body="Usage with no published plan is reported as unpriced on the invoice — never billed at zero."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'ledger' ? (
          <>
            <SutrCard>
              <SutrCardBody className="sutr-muted">
                Append-only. A correction is another entry, and voiding an invoice reverses its
                entries rather than deleting them.
              </SutrCardBody>
            </SutrCard>
            {ledger.error ? <SutrError what={ledger.error} /> : null}
            {ledger.data ? (
              ledger.data.entries.length ? (
                <>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
                      gap: 12,
                    }}
                  >
                    <SutrMetric
                      label="Receivable"
                      value={formatMicros(
                        ledger.data.receivable?.balance_micros,
                        ledger.data.receivable?.currency,
                      )}
                      foot="owed to this platform"
                    />
                    <SutrMetric
                      label="Payable"
                      value={formatMicros(
                        ledger.data.payable?.balance_micros,
                        ledger.data.payable?.currency,
                      )}
                      foot="owed to providers"
                    />
                  </div>
                  <SutrTable
                    columns={ledgerColumns}
                    rows={ledger.data.entries}
                    rowKey={(row) => row.id}
                    minWidth={900}
                  />
                </>
              ) : (
                <SutrEmpty
                  title="The ledger is empty"
                  body="Entries appear when an invoice is issued."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'settlements' ? (
          <>
            {settlements.error ? <SutrError what={settlements.error} /> : null}
            {settlements.data ? (
              settlements.data.length ? (
                <SutrTable
                  columns={settlementColumns}
                  rows={settlements.data}
                  rowKey={(row) => row.id}
                  minWidth={900}
                />
              ) : (
                <SutrEmpty
                  title="No settlement runs"
                  body="A settlement computes what a provider has earned from a period. The share is configuration, recorded on the run."
                />
              )
            ) : null}
          </>
        ) : null}

        {capabilities.data ? (
          <>
            <SutrSectionLabel>What this install cannot do</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                <pre className="sutr-muted" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                  {JSON.stringify(capabilities.data, null, 2)}
                </pre>
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={openInvoice !== null}
        onClose={() => setOpenInvoice(null)}
        title="Invoice"
        subtitle={invoice.data?.number ?? openInvoice ?? undefined}
        wide
      >
        {invoice.loading ? <SutrSpinner /> : null}
        {invoice.error ? <SutrError what={invoice.error} /> : null}
        {invoice.data ? (
          <>
            <SutrDefinitionList
              items={[
                { key: 'State', value: invoice.data.state },
                {
                  key: 'Period',
                  value: `${formatWhen(invoice.data.period_start)} → ${formatWhen(invoice.data.period_end)}`,
                },
                {
                  key: 'Total',
                  value: formatMicros(invoice.data.total_micros, invoice.data.currency),
                },
                {
                  key: 'Collected',
                  value: invoice.data.collected_by_this_platform
                    ? 'yes'
                    : 'not by this platform — no processor is wired',
                },
              ]}
            />

            <SutrSectionLabel>Lines</SutrSectionLabel>
            {invoice.data.lines?.length ? (
              <SutrTable
                columns={[
                  {
                    key: 'description',
                    header: 'Description',
                    render: (row) => String(row.description ?? row.usage_kind ?? ''),
                  },
                  {
                    key: 'quantity',
                    header: 'Quantity',
                    numeric: true,
                    width: 100,
                    render: (row) => String(row.quantity ?? ''),
                  },
                  {
                    key: 'plan',
                    header: 'Plan',
                    // The plan the line was priced by, exactly: key, version and
                    // model. An invoice has to be answerable a year later, and
                    // "the standard plan" is not an answer when it has changed.
                    render: (row) => {
                      const plan = row.plan as
                        | { key?: string; version?: number; model?: string }
                        | undefined
                      if (!plan) return <span className="sutr-muted">unpriced</span>
                      return (
                        <span className="sutr-muted">
                          {plan.key} v{plan.version} ({plan.model})
                        </span>
                      )
                    },
                  },
                  {
                    key: 'arithmetic',
                    header: 'How it was computed',
                    render: (row) => {
                      const breakdown = row.breakdown as { steps?: string[] } | undefined
                      if (!breakdown?.steps?.length) return ''
                      return <span className="sutr-muted">{breakdown.steps.join(' · ')}</span>
                    },
                  },
                  {
                    key: 'subtotal',
                    header: 'Subtotal',
                    numeric: true,
                    width: 140,
                    render: (row) =>
                      formatMicros(row.subtotal_micros as number, invoice.data?.currency),
                  },
                ]}
                rows={invoice.data.lines}
                rowKey={(row) => String(row.description ?? JSON.stringify(row).slice(0, 48))}
                minWidth={900}
              />
            ) : (
              <p className="sutr-muted">No priced lines.</p>
            )}

            {invoice.data.unpriced?.length ? (
              <>
                <SutrSectionLabel>Unpriced usage</SutrSectionLabel>
                <SutrCard>
                  <SutrCardBody>
                    <p className="sutr-muted">
                      This usage happened and has no published plan, so it is reported rather than
                      billed at zero or dropped.
                    </p>
                    <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                      {JSON.stringify(invoice.data.unpriced, null, 2)}
                    </pre>
                  </SutrCardBody>
                </SutrCard>
              </>
            ) : null}
          </>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}

import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import React from 'react';
import { AiClaimComparisonDashboard } from '../components/ai/AiClaimComparisonDashboard';

vi.mock('../services/aiAnalyticsApi', () => ({
  aiAnalyticsApi: {
    getClaimComparison: vi.fn(),
    getClaimComparisonSchemaCheck: vi.fn(),
  },
}));

vi.mock('../services/api', () => ({
  api: {
    getServerDate: vi.fn().mockResolvedValue('2026-08-13'),
    getDateRange: vi.fn().mockResolvedValue({ server_date: '2026-08-13', start_date: '2026-01-01', end_date: '2026-12-31' }),
  },
}));

import { aiAnalyticsApi } from '../services/aiAnalyticsApi';

const breakdown = (overrides: Partial<Record<string, number | null>> = {}) => ({
  total: 0,
  paid: 0,
  ready_to_be_paid: 0,
  under_review: 0,
  in_progress: 0,
  denied: 0,
  not_billable: 0,
  paid_rate: null,
  resolved_success_rate: null,
  denied_rate: null,
  ...overrides,
});

const WEEK_COUNTS = (overrides: Record<string, number | null> = {}) => ({
  paid: 0,
  ready_to_be_paid: 0,
  denied: 0,
  resolved_success_rate: null,
  ...overrides,
});

type MarkedClaimOverrides = Partial<{
  claim_id: number;
  run_number: string;
  invoice_number: string;
  amount_invoiced: number;
  services_total: number | null;
  department_allows_direct_billing: boolean;
  not_billable_by: number | null;
  not_billable_by_name: string | null;
  status_when_not_billable: string;
}>;

const markedClaim = (overrides: MarkedClaimOverrides = {}) => ({
  claim_id: 2,
  run_number: 'R200',
  cohort: 'other' as const,
  subtype: 'manual' as const,
  department_id: 8,
  department_name: 'Dept B',
  department_allows_direct_billing: false,
  incident: 'Motor Vehicle Incident',
  status: 'Denied',
  bucket: 'denied',
  billing_target: 'insurance' as const,
  insurance_company: 'Acme Ins',
  adjuster_name: 'Bob',
  invoice_number: 'INV-2',
  amount_invoiced: 800,
  ai_invoice_total: null,
  services_total: 800,
  ever_not_billable: true,
  not_billable_by: 77,
  not_billable_by_name: 'Pat Smith',
  not_billable_at: '2026-03-05T00:00:00',
  status_when_not_billable: 'Assigned',
  submitted_at: '2026-02-12T00:00:00',
  sent_to_insurance_at: '2026-03-06T00:00:00',
  days_submitted_to_insurance: 22,
  ...overrides,
});

const COMPARISON = {
  period_start: '2026-01-01',
  period_end: '2026-08-13',
  // `total` = finished claims only (paid + ready + denied), matching the
  // backend contract after this PR.
  overall: breakdown({ total: 7, paid: 4, ready_to_be_paid: 2, denied: 1, under_review: 1, in_progress: 1, not_billable: 1, paid_rate: 85.71, resolved_success_rate: 85.71, denied_rate: 14.29 }),
  ai_released: breakdown({ total: 4, paid: 3, ready_to_be_paid: 1, denied: 0, in_progress: 1, paid_rate: 100.0, resolved_success_rate: 100.0, denied_rate: 0.0 }),
  other: breakdown({ total: 3, paid: 1, ready_to_be_paid: 1, denied: 1, under_review: 1, not_billable: 1, paid_rate: 66.67, resolved_success_rate: 66.67, denied_rate: 33.33 }),
  other_manual: breakdown({ total: 2, paid: 1, ready_to_be_paid: 1, under_review: 1, paid_rate: 100.0, resolved_success_rate: 100.0, denied_rate: 0.0 }),
  other_ai_rejected: breakdown({ total: 1, denied: 1, not_billable: 1, paid_rate: 0.0, resolved_success_rate: 0.0, denied_rate: 100.0 }),
  ai_pending: 7,
  deleted_runs: 4,
  unresolved_claims: 0,
  weekly: [
    {
      week: '2026-W06',
      week_start: '2026-02-02',
      ai_released: WEEK_COUNTS({ paid: 1 , resolved_success_rate: 100.0 }),
      other: WEEK_COUNTS(),
      other_manual: WEEK_COUNTS(),
      other_ai_rejected: WEEK_COUNTS(),
    },
    {
      week: '2026-W07',
      week_start: '2026-02-09',
      ai_released: WEEK_COUNTS(),
      other: WEEK_COUNTS({ paid: 1, denied: 1, resolved_success_rate: 50.0 }),
      other_manual: WEEK_COUNTS({ paid: 1, resolved_success_rate: 100.0 }),
      other_ai_rejected: WEEK_COUNTS({ denied: 1, resolved_success_rate: 0.0 }),
    },
  ],
  by_incident_type: [
    {
      label: 'Motor Vehicle Incident',
      overall: breakdown({ total: 3, paid: 2, ready_to_be_paid: 1, paid_rate: 66.67, resolved_success_rate: 100.0, denied_rate: 0.0 }),
      ai_released: breakdown({ total: 3, paid: 2, ready_to_be_paid: 1, paid_rate: 66.67, resolved_success_rate: 100.0, denied_rate: 0.0 }),
      other: breakdown(),
    },
  ],
  by_billing_target: [
    {
      label: 'Insurance',
      overall: breakdown({ total: 2, denied: 1, paid: 1, paid_rate: 50.0, resolved_success_rate: 50.0, denied_rate: 50.0 }),
      ai_released: breakdown(),
      other: breakdown({ total: 2, denied: 1, paid: 1, paid_rate: 50.0, resolved_success_rate: 50.0, denied_rate: 50.0 }),
    },
  ],
  not_billable: {
    ai_released: {
      ever_not_billable: 1,
      effective_status: { in_progress: 1 },
      ready_or_paid: 0,
      denied: 0,
      ready_vs_denied_rate: null,
    },
    other: {
      ever_not_billable: 3,
      effective_status: { paid: 1, ready_to_be_paid: 1, denied: 1 },
      ready_or_paid: 2,
      denied: 1,
      ready_vs_denied_rate: 66.67,
    },
    marked_by_processor: 4,
    identified_by_system: 2,
  },
  claims: [
    {
      claim_id: 1,
      run_number: 'R100',
      cohort: 'ai_released',
      subtype: null,
      department_id: 7,
      department_name: 'Dept A',
      department_allows_direct_billing: true,
      incident: 'Motor Vehicle Incident',
      status: 'Payment Approved',
      bucket: 'ready_to_be_paid',
      billing_target: 'insurance',
      insurance_company: 'Acme Ins',
      adjuster_name: 'Jane',
      invoice_number: 'INV-1',
      amount_invoiced: 1200,
      ai_invoice_total: 1500,
      services_total: 1100,
      ever_not_billable: false,
      not_billable_by: null,
      not_billable_by_name: null,
      not_billable_at: null,
      status_when_not_billable: null,
      submitted_at: '2026-02-10T00:00:00',
      sent_to_insurance_at: '2026-03-01T00:00:00',
      days_submitted_to_insurance: 19,
    },
    markedClaim(),
    markedClaim({
      claim_id: 3,
      run_number: 'R300',
      invoice_number: 'INV-3',
      amount_invoiced: 300,
      services_total: 300,
      department_allows_direct_billing: true,
      status_when_not_billable: 'Queued',
    }),
    markedClaim({
      claim_id: 4,
      run_number: 'R400',
      invoice_number: 'INV-4',
      amount_invoiced: 450,
      services_total: 450,
      not_billable_by: 88,
      not_billable_by_name: null,
      status_when_not_billable: 'Review',
    }),
    markedClaim({
      claim_id: 5,
      run_number: 'R500',
      invoice_number: 'INV-5',
      amount_invoiced: 0,
      services_total: null,
      not_billable_by: null,
      not_billable_by_name: null,
    }),
  ],
  unmapped_statuses: {},
  resolved_columns: { claims_row_start: 'SysStartTime' },
  source_status: { recoveryhub_sql: 'available', recoveryhub_ai_mongo: 'available' },
  data_complete: true,
};

const SCHEMA = {
  period_start: '2026-01-01',
  period_end: '2026-08-13',
  status_values: [
    { status: 'Payment Received', claim_count: 5, bucket: 'paid', mapped: true },
    { status: 'Mystery Stage', claim_count: 2, bucket: 'in_progress', mapped: false },
  ],
  run_type_values: [
    { type_id: 1, type_name: 'Motor Vehicle Incident', label: 'Motor Vehicle Incident', claim_count: 7 },
  ],
  resolved_columns: { claims_row_start: 'SysStartTime' },
  claims_deleted_columns: ['id', 'timestamp'],
  source_status: { recoveryhub_sql: 'available' },
  data_complete: true,
};

describe('AiClaimComparisonDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockResolvedValue(COMPARISON);
    (aiAnalyticsApi.getClaimComparisonSchemaCheck as ReturnType<typeof vi.fn>).mockResolvedValue(SCHEMA);
  });

  test('shows loading state initially', () => {
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<AiClaimComparisonDashboard />);
    expect(screen.getByText(/Loading claim payment comparison/i)).toBeInTheDocument();
  });

  test('renders KPI cards for overall, cohort rates, deleted and pending counts', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Overall Paid Rate')).toBeInTheDocument());
    expect(screen.getByText('AI-Released Paid Rate')).toBeInTheDocument();
    expect(screen.getAllByText('Other Paid Rate').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Claims Compared')).toBeInTheDocument();
    expect(screen.getByText('Deleted Runs')).toBeInTheDocument();
    expect(screen.getByText('AI Pending (excluded)')).toBeInTheDocument();
    expect(screen.getAllByText('4').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('7').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('100.0%').length).toBeGreaterThanOrEqual(1);
  });

  test('status shares divide by the whole displayed population', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Final Payment Outcome by Cohort')).toBeInTheDocument());
    // other_manual: total 2 + under_review 1 = population 3 -> paid share 1/3 = 33.3%
    // (under_review/in_progress columns must not inflate shares past 100%)
    expect(screen.getAllByText('(33.3%)').length).toBeGreaterThanOrEqual(1);
  });

  test('renders AI-released vs other cohort breakdown with manual and AI-rejected split', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Final Payment Outcome by Cohort')).toBeInTheDocument());
    expect(screen.getByText('AI-Released')).toBeInTheDocument();
    expect(screen.getByText('Other · Manual')).toBeInTheDocument();
    expect(screen.getByText('Other · AI-Rejected')).toBeInTheDocument();
    expect(screen.getByText(/AI-pending claims/)).toBeInTheDocument();
  });

  test('renders weekly outcomes table with ISO week rows', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Weekly Final Outcomes (by effective date)')).toBeInTheDocument());
    expect(screen.getByText(/2026-W07/)).toBeInTheDocument();
    expect(screen.getByText(/2026-W06/)).toBeInTheDocument();
  });

  test('renders incident type and billing target sections', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('By Incident Type')).toBeInTheDocument());
    expect(screen.getAllByText('Motor Vehicle Incident').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Paid + ready to be paid')).toBeInTheDocument();
    expect(screen.getAllByText('AI').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('By Billing Target')).toBeInTheDocument();
    expect(screen.getByText('Insurance')).toBeInTheDocument();
  });

  test('renders not billable section with ready-vs-denied rate and separate system count', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Not Billable')).toBeInTheDocument());
    expect(screen.getAllByText('66.7%').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Marked by processor (claim status)')).toBeInTheDocument();
    expect(screen.getByText('Identified by system (AI billability)')).toBeInTheDocument();
    expect(screen.getByText('AI-released runs')).toBeInTheDocument();
    expect(screen.getByText('Other runs')).toBeInTheDocument();
  });

  test('shows data incomplete warning when data_complete is false', async () => {
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...COMPARISON,
      data_complete: false,
      source_status: { recoveryhub_sql: 'unavailable', recoveryhub_ai_mongo: 'available' },
    });
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText(/Data may be incomplete/i)).toBeInTheDocument());
    expect(screen.getByText(/recoveryhub_sql=unavailable/i)).toBeInTheDocument();
  });

  test('shows unmapped status banner and loads schema check on demand', async () => {
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...COMPARISON,
      unmapped_statuses: { 'Mystery Stage': 2 },
    });
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText(/Mystery Stage \(2\)/)).toBeInTheDocument());
    expect(aiAnalyticsApi.getClaimComparisonSchemaCheck).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText('Open schema check'));
    await waitFor(() => expect(screen.getByText('Claims.status (in scope)')).toBeInTheDocument());
    expect(aiAnalyticsApi.getClaimComparisonSchemaCheck).toHaveBeenCalledTimes(1);
    expect(screen.getByText('no — counted as In progress')).toBeInTheDocument();
  });

  test('shows error state on fetch failure', async () => {
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('comparison fetch failed'),
    );
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText(/comparison fetch failed/i)).toBeInTheDocument());
  });

  test('drills down incident → department → insurer → claim detail', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Claim Detail Drill-Down')).toBeInTheDocument());

    const drill = within(screen.getByText('Claim Detail Drill-Down').parentElement as HTMLElement);
    // Level 1: aggregation by incident type
    expect(drill.getAllByText('Motor Vehicle Incident').length).toBeGreaterThan(0);
    expect(drill.getByText('All incident types')).toBeInTheDocument();
    // Collected-$ breakdown columns are present on every drill level
    expect(drill.getByText('AI collected')).toBeInTheDocument();
    expect(drill.getByText('Other collected')).toBeInTheDocument();
    fireEvent.click(drill.getByRole('button', { name: /Motor Vehicle Incident/ }));

    // Level 2: departments within the incident (grouped by dept id)
    await waitFor(() => expect(drill.getByText('Dept A (#7)')).toBeInTheDocument());
    expect(drill.getByText('Dept B (#8)')).toBeInTheDocument();
    fireEvent.click(drill.getByText('Dept A (#7)'));

    // Level 3: insurers within the department
    await waitFor(() => expect(drill.getAllByText('Acme Ins').length).toBeGreaterThan(0));
    fireEvent.click(drill.getAllByText('Acme Ins')[0]);

    // Level 4: processors within the insurer (not-billable marker user)
    await waitFor(() => expect(drill.getByText('No not-billable marker')).toBeInTheDocument());
    fireEvent.click(drill.getByText('No not-billable marker'));

    // Level 5: claim rows for the AI claim under Dept A / Acme Ins
    await waitFor(() => expect(drill.getByText('R100')).toBeInTheDocument());
    expect(drill.queryByText('R200')).not.toBeInTheDocument();

    // Expanded row shows per-claim details
    fireEvent.click(drill.getByLabelText('Toggle claim 1 detail'));
    await waitFor(() => expect(drill.getByText('INV-1')).toBeInTheDocument());
    expect(drill.getByText('Jane')).toBeInTheDocument();
    expect(drill.getByText('19')).toBeInTheDocument();
  });

  test('uses the marker user name in the processor drill-down', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Claim Detail Drill-Down')).toBeInTheDocument());
    const drill = within(screen.getByText('Claim Detail Drill-Down').parentElement as HTMLElement);
    fireEvent.click(drill.getByRole('button', { name: /Motor Vehicle Incident/ }));
    fireEvent.click(await drill.findByText('Dept B (#8)'));
    fireEvent.click((await drill.findAllByText('Acme Ins'))[0]);
    expect(await drill.findByText('Pat Smith (#77)')).toBeInTheDocument();
    fireEvent.click(drill.getByText('Pat Smith (#77)'));
    expect(await drill.findByText('R200')).toBeInTheDocument();
    expect(drill.getByRole('columnheader', { name: 'Days submitted → sent to insurance' }))
      .toHaveAttribute('title', 'Days from when the claim was submitted to us until its invoice was sent to the insurance company');
  });

  test('shows collected totals with per-invoice averages and the not-billable review', async () => {
    render(<AiClaimComparisonDashboard />);
    await waitFor(() => expect(screen.getByText('Claim Detail Drill-Down')).toBeInTheDocument());
    // AI collected $1,200 over 1 paid/ready claim -> $1,200 / $1,200
    expect(screen.getByText('$1,200 / $1,200')).toBeInTheDocument();
    // Other's only claim was denied -> $0 / —
    expect(screen.getByText('$0 / —')).toBeInTheDocument();

    // Not-billable groups start collapsed and summarize claims and line items.
    const review = within(screen.getByText('Not-Billable Claims Review').parentElement as HTMLElement);
    expect(review.getByText('Pat Smith (#77) · 2 claims · $1,100')).toBeInTheDocument();
    expect(review.getByText('User 88 · 1 claim · $450')).toBeInTheDocument();
    expect(review.getByText('No user recorded · 1 claim · $0')).toBeInTheDocument();
    expect(review.getAllByRole('button', { name: /^Toggle not-billable group/ }).map((button) => button.getAttribute('aria-label')))
      .toEqual([
        'Toggle not-billable group Pat Smith (#77)',
        'Toggle not-billable group User 88',
        'Toggle not-billable group No user recorded',
      ]);
    expect(review.queryByText('R200')).not.toBeInTheDocument();
    expect(review.queryByText('R400')).not.toBeInTheDocument();
    expect(review.queryByText('R500')).not.toBeInTheDocument();
    expect(review.queryByText('R100')).not.toBeInTheDocument();
    fireEvent.click(review.getByRole('button', { name: 'Toggle not-billable group Pat Smith (#77)' }));
    expect(review.getByText('R200')).toBeInTheDocument();
    expect(review.getByText('R300')).toBeInTheDocument();
    expect(review.getByText('Assigned')).toBeInTheDocument();
    expect(review.getByText('not allowed')).toBeInTheDocument();
    expect(review.getByText('$800')).toBeInTheDocument();
    expect(review.getByRole('columnheader', { name: 'Days submitted → sent to insurance' }))
      .toHaveAttribute('title', 'Days from when the claim was submitted to us until its invoice was sent to the insurance company');

    fireEvent.click(review.getByRole('button', { name: 'Expand all' }));
    expect(review.getByText('R400')).toBeInTheDocument();
    expect(review.getByText('R500')).toBeInTheDocument();
    expect(review.getByRole('button', { name: 'Collapse all' })).toBeInTheDocument();
  });

  test('shows the not-billable review empty state when no claims were marked', async () => {
    (aiAnalyticsApi.getClaimComparison as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ...COMPARISON,
      claims: COMPARISON.claims.map((claim) => ({ ...claim, ever_not_billable: false })),
    });
    render(<AiClaimComparisonDashboard />);
    expect(await screen.findByText('Not-Billable Claims Review')).toBeInTheDocument();
    expect(screen.getByText('No claims in this period were marked not-billable.')).toBeInTheDocument();
  });
});

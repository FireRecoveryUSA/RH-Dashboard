import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
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
});

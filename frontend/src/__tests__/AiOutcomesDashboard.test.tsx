import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { AiOutcomesDashboard } from '../components/ai/AiOutcomesDashboard';

vi.mock('../services/aiAnalyticsApi', () => ({
  aiAnalyticsApi: {
    getOutcomeSummary: vi.fn(),
    getOutcomeFunnel: vi.fn(),
    getRejectionReasons: vi.fn(),
    getDepartmentOutcomes: vi.fn(),
    getBillabilityStats: vi.fn(),
    getInvoiceCohort: vi.fn(),
    getInvoiceTrace: vi.fn(),
  },
}));

vi.mock('../services/api', () => ({
  api: {
    getServerDate: vi.fn().mockResolvedValue('2026-08-13'),
    getDateRange: vi.fn().mockResolvedValue({ server_date: '2026-08-13', start_date: '2026-08-09', end_date: '2026-08-15' }),
  },
}));

import { aiAnalyticsApi } from '../services/aiAnalyticsApi';

const SUMMARY = {
  total_ai_invoices: 100,
  did_not_qualify: 12,
  released: 60,
  cancelled_rejected: 20,
  pending: 15,
  unknown: 5,
  terminal_count: 80,
  business_release_rate: 75.0,
  rejection_rate: 25.0,
  ai_completed: 90,
  ai_failed: 5,
  ai_not_enabled: 5,
  writeback_success: 55,
  writeback_failed: 5,
  avg_confidence: 88,
  source_status: { recoveryhub_sql: 'available', recoveryhub_ai_mongo: 'available' },
  data_complete: true,
};

const FUNNEL = [
  { stage: 'AI Processing', count: 100, description: 'All invoices' },
  {
    stage: 'Step 1: category & fee item results',
    count: 90,
    description: 'AI records grouped by category and fee item.',
    breakdown: [
      {
        label: 'Structure Fires',
        count: 40,
        match_status: 'category_matched',
        description: 'Category matches current fee schedule · Intake: Category identified',
      },
      {
        label: 'Structure Fires / Vehicle Fire',
        count: 50,
        match_status: 'matched',
        description: 'Category and fee item match current fee schedule',
      },
    ],
  },
  { stage: 'Business Reviewed', count: 80, description: 'Human reviewed' },
  { stage: 'Released', count: 60, description: 'Invoice released' },
];

const REJECTION_REASONS = [
  {
    normalized_category: 'documentation',
    count: 10,
    percent: 50.0,
    raw_reason_breakdown: [
      { raw_reason: 'Missing docs', count: 7 },
      { raw_reason: 'Incomplete form', count: 3 },
    ],
  },
  {
    normalized_category: 'eligibility',
    count: 10,
    percent: 50.0,
    raw_reason_breakdown: [{ raw_reason: 'Not eligible', count: 10 }],
  },
];

const DEPARTMENTS = [
  {
    department_id: 5,
    department_name: 'Metro Fire',
    state: 'CA',
    volume: 50,
    released: 30,
    rejected: 10,
    pending: 10,
    release_rate: 60.0,
    ai_completion_rate: 90.0,
    writeback_failure_rate: 5.0,
    avg_confidence: 85,
    retry_count: 2,
    human_intervention_count: 1,
  },
];

const BILLABILITY = {
  ai_records: 100,
  billability_determined: 80,
  billability_undetermined: 20,
  billable: 70,
  not_billable: 10,
  billing_category_distribution: { billable: 70, not_billable: 10 },
};

const COHORT = {
  invoices: [],
  total_count: 0,
  page: 1,
  page_size: 50,
  source_status: {},
  data_complete: true,
};

describe('AiOutcomesDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (aiAnalyticsApi.getOutcomeSummary as ReturnType<typeof vi.fn>).mockResolvedValue(SUMMARY);
    (aiAnalyticsApi.getOutcomeFunnel as ReturnType<typeof vi.fn>).mockResolvedValue(FUNNEL);
    (aiAnalyticsApi.getRejectionReasons as ReturnType<typeof vi.fn>).mockResolvedValue(REJECTION_REASONS);
    (aiAnalyticsApi.getDepartmentOutcomes as ReturnType<typeof vi.fn>).mockResolvedValue(DEPARTMENTS);
    (aiAnalyticsApi.getBillabilityStats as ReturnType<typeof vi.fn>).mockResolvedValue(BILLABILITY);
    (aiAnalyticsApi.getInvoiceCohort as ReturnType<typeof vi.fn>).mockResolvedValue(COHORT);
  });

  test('shows loading state initially', () => {
    (aiAnalyticsApi.getOutcomeSummary as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<AiOutcomesDashboard />);
    expect(screen.getByText(/Loading AI outcomes/i)).toBeInTheDocument();
  });

  test('renders KPI cards with summary values', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('AI-Eligible Claims')).toBeInTheDocument());
    expect(screen.getByText('Did Not Qualify')).toBeInTheDocument();
    expect(screen.getByText('No qualifying AI fee tile')).toBeInTheDocument();
    expect(screen.getByText('Business Release Rate')).toBeInTheDocument();
    expect(screen.getByText('Rejection Rate')).toBeInTheDocument();
    expect(screen.getByText('Writeback Success')).toBeInTheDocument();
    expect(screen.getByText('Avg Confidence')).toBeInTheDocument();
    // "Pending" and "AI Completed" appear as both KPI labels and funnel stages,
    // so use getAllByText to verify they render at least once.
    expect(screen.getAllByText('Pending').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('AI Completed').length).toBeGreaterThanOrEqual(1);
  });

  test('renders funnel with stage names and counts', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('AI Invoice Pipeline Funnel')).toBeInTheDocument());
    expect(screen.getByText('AI Processing')).toBeInTheDocument();
    expect(screen.getByText('Business Reviewed')).toBeInTheDocument();
    // "AI Completed" and "Released" also appear as KPI labels — verify they exist
    expect(screen.getAllByText('AI Completed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Released').length).toBeGreaterThanOrEqual(1);
  });

  test('renders step-1 breakdown with category labels and descriptions', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('AI Invoice Pipeline Funnel')).toBeInTheDocument());
    // Actual fee-schedule category/item labels, not the old generic bucket.
    expect(screen.getByText('Structure Fires')).toBeInTheDocument();
    expect(screen.getByText('Structure Fires / Vehicle Fire')).toBeInTheDocument();
    expect(
      screen.getByText('Category matches current fee schedule · Intake: Category identified')
    ).toBeInTheDocument();
    expect(screen.queryByText('Category identified (no level)')).not.toBeInTheDocument();
  });

  test('renders rejection reasons with normalized categories', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('Rejection Reasons (Normalized)')).toBeInTheDocument());
    // normalized_category has underscores replaced with spaces
    expect(screen.getByText('documentation')).toBeInTheDocument();
    expect(screen.getByText('eligibility')).toBeInTheDocument();
    // Raw reason breakdown (only shown when > 1 breakdown entry)
    expect(screen.getByText(/Missing docs/)).toBeInTheDocument();
  });

  test('renders department comparison table', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('Top Departments by AI Invoice Volume')).toBeInTheDocument());
    expect(screen.getByText('Metro Fire')).toBeInTheDocument();
    expect(screen.getByText('CA')).toBeInTheDocument();
  });

  test('renders billability section', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('Incident / Billability Evaluation')).toBeInTheDocument());
    expect(screen.getByText('AI Records')).toBeInTheDocument();
    expect(screen.getByText('Billability Determined')).toBeInTheDocument();
    expect(screen.getByText('Billable')).toBeInTheDocument();
  });

  test('shows error state on fetch failure', async () => {
    (aiAnalyticsApi.getOutcomeSummary as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('outcomes fetch failed')
    );
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText(/outcomes fetch failed/i)).toBeInTheDocument());
  });

  test('shows data incomplete warning when data_complete is false', async () => {
    (aiAnalyticsApi.getOutcomeSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...SUMMARY,
      data_complete: false,
      source_status: { recoveryhub_sql: 'unavailable', recoveryhub_ai_mongo: 'available' },
    });
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText(/Data may be incomplete/i)).toBeInTheDocument());
    expect(screen.getByText(/recoveryhub_sql=unavailable/i)).toBeInTheDocument();
  });

  test('shows "Billing Not Enabled" KPI when ai_not_enabled > 0', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('Billing Not Enabled')).toBeInTheDocument());
  });

  test('hides "Billing Not Enabled" KPI when ai_not_enabled is 0', async () => {
    (aiAnalyticsApi.getOutcomeSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...SUMMARY,
      ai_not_enabled: 0,
    });
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('AI-Eligible Claims')).toBeInTheDocument());
    expect(screen.queryByText('Billing Not Enabled')).not.toBeInTheDocument();
  });

  test('renders filter bar with range type selector', async () => {
    render(<AiOutcomesDashboard />);
    await waitFor(() => expect(screen.getByText('Range')).toBeInTheDocument());
    expect(screen.getByText('Period')).toBeInTheDocument();
  });
});

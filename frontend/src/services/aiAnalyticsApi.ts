import { createApiFetch } from './fetchWrapper';

// ---------------------------------------------------------------------------
// Types — mirror the backend Pydantic models
// ---------------------------------------------------------------------------

export interface AiAnalyticsFilters {
  start_date?: string;
  end_date?: string;
  department_id?: number;
  business_outcome?: string;
  ai_processing_status?: string;
  agent_execution_status?: string;
  confidence_min?: number;
  confidence_max?: number;
  has_retry?: boolean;
  writeback_status?: string;
  billing_category?: string;
  reason_category?: string;
  page?: number;
  page_size?: number;
  sort_by?: string;
  sort_direction?: string;
  date_basis?: string;
}

export interface AiOutcomeSummary {
  total_ai_invoices: number;
  did_not_qualify: number;
  released: number;
  cancelled_rejected: number;
  pending: number;
  unknown: number;
  terminal_count: number;
  business_release_rate: number;
  rejection_rate: number;
  ai_completed: number;
  ai_failed: number;
  ai_not_enabled: number;
  writeback_success: number;
  writeback_failed: number;
  avg_confidence: number | null;
  source_status: Record<string, string>;
  data_complete: boolean;
}

export interface AiPipelineStageBreakdown {
  label: string;
  count: number;
  match_status?: string | null;
  description?: string;
}

export interface AiPipelineStageStat {
  stage: string;
  count: number;
  description: string;
  breakdown?: AiPipelineStageBreakdown[] | null;
}

export interface AiOutcomeTrendPoint {
  period: string;
  total: number;
  released: number;
  rejected: number;
  pending: number;
  release_rate: number | null;
}

export interface AiRejectionReasonBreakdown {
  raw_reason: string;
  count: number;
}

export interface AiRejectionReasonStat {
  normalized_category: string;
  count: number;
  percent: number;
  raw_reason_breakdown: AiRejectionReasonBreakdown[];
}

export interface AiDepartmentOutcomeStat {
  department_id: number;
  department_name: string | null;
  state: string | null;
  volume: number;
  released: number;
  rejected: number;
  pending: number;
  release_rate: number | null;
  ai_completion_rate: number | null;
  writeback_failure_rate: number | null;
  avg_confidence: number | null;
  retry_count: number;
  human_intervention_count: number;
}

export interface AiBillabilityStat {
  ai_records: number;
  billability_determined: number;
  billability_undetermined: number;
  billable: number;
  not_billable: number;
  billing_category_distribution: Record<string, number>;
}

export interface AiInvoiceListItem {
  claim_id: number;
  invoice_number: string | null;
  department_id: number | null;
  department_name: string | null;
  run_number: string | null;
  claim_created_at: string | null;
  ai_business_updated_at: string | null;
  business_outcome: string;
  raw_rejection_reason: string | null;
  raw_rejection_description: string | null;
  normalized_rejection_category: string | null;
  ai_processing_status: string | null;
  agent_execution_status: string | null;
  is_billable: boolean | null;
  billing_category: string | null;
  confidence: number | null;
  writeback_status: string;
  retry_count: number;
  thread_id: string | null;
  ai_record_state: string;
  business_record_state: string;
  invoice_total: number | null;
  amount_invoiced: number | null;
  processing_time_seconds: number | null;
}

export interface AiInvoiceCohortResponse {
  invoices: AiInvoiceListItem[];
  total_count: number;
  page: number;
  page_size: number;
  source_status: Record<string, string>;
  data_complete: boolean;
}

// Diagnostics types

export interface AiDiagnosticsSummary {
  ai_runs: number;
  did_not_qualify: number;
  completed: number;
  errors: number;
  retries: number;
  retry_success: number;
  low_confidence: number;
  writeback_failures: number;
  avg_duration: number | null;
  p50_duration: number | null;
  p90_duration: number | null;
  p95_duration: number | null;
  source_status: Record<string, string>;
  data_complete: boolean;
}

export interface AiConfidenceBucketStat {
  bucket: string;
  count: number;
  released: number;
  rejected: number;
  pending: number;
  release_rate: number | null;
}

export interface AiAgentStat {
  agent: string;
  status: string;
  processing_stage: string;
  request_type: string;
  count: number;
  avg_execution_time: number | null;
}

export interface AiStatusDistributionItem {
  dimension: string;
  value: string;
  count: number;
}

export interface AiRetryAnalysis {
  total_records: number;
  records_with_retries: number;
  retry_rate: number;
  retry_success_rate: number | null;
  retry_count_distribution: Record<string, number>;
  retried_outcome_distribution: Record<string, number>;
}

export interface AiWritebackAnalysis {
  total_records: number;
  status_distribution: Record<string, number>;
  failure_count: number;
  failure_rate: number;
  failure_by_processing_status: Record<string, number>;
}

// Invoice trace types

export interface AiConversationRecord {
  conversation_id: string;
  agent: string | null;
  status: string | null;
  created_at: string | null;
  processing_stage: string | null;
  request_type: string | null;
  execution_time_seconds: number | null;
  input_data: any;
  incident_json: any;
  results: any;
  output_data: any;
}

export interface AiLineItemEntry {
  item: string | null;
  description: string | null;
  quantity: number | null;
  rate: number | null;
  line_item_total: number | null;
  resources: Record<string, any>[];
}

export interface AiFinalLineItemEntry {
  claim_service_id: number;
  item: string | null;
  rate: number | null;
  quantity: number | null;
  description: string | null;
  resources: Record<string, any>[];
}

export interface AiLineItemComparison {
  ai_original_amount: number | null;
  final_rh_amount: number | null;
  difference: number | null;
  ai_only_items: string[];
  rh_only_items: string[];
  quantity_changes: Record<string, any>[];
  rate_changes: Record<string, any>[];
}

export interface AiProcessLog {
  id: number;
  log_text: string | null;
  user_id: number;
  user_type_id: number;
  created_date: string | null;
}

export interface AiInvoiceTrace {
  claim_id: number;
  invoice_number: string | null;
  run_number: string | null;
  department_id: number | null;
  department_name: string | null;
  claim_created_at: string | null;
  alarm_received: string | null;
  call_cleared: string | null;
  recoveryhub_claim_status: string | null;
  business_outcome: string;
  ai_inv_process_status: number | null;
  business_status_updated_at: string | null;
  process_logs: AiProcessLog[];
  cancellation_reason: string | null;
  cancellation_description: string | null;
  cancellation_date: string | null;
  business_user_id: number | null;
  ai_record_state: string;
  claim_processing_status: string | null;
  agent_exec_status: string | null;
  inserted_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  billing_category: string | null;
  incident_duration_in_minutes: number | null;
  confidence_level: number | null;
  review_msg: string | null;
  line_items_save_to_rh_status: boolean | null;
  invoice_total: number | null;
  processing_time_seconds: number | null;
  retry_count: number;
  conversation_id: string | null;
  thread_id_is_billable: string | null;
  thread_id: string | null;
  retry_thread_id: string | null;
  conversations: AiConversationRecord[];
  ai_line_items: AiLineItemEntry[];
  final_line_items: AiFinalLineItemEntry[];
  comparison: AiLineItemComparison | null;
  raw_ai_record: Record<string, any> | null;
  source_status: Record<string, string>;
  data_complete: boolean;
}

// Sync health types (Phase 11)

export type SyncStatus =
  | 'synced'
  | 'syncing'
  | 'catching-up'
  | 'divergence-detected'
  | 'error'
  | 'stopped'
  | 'unknown';

export interface AiSyncIntegrity {
  last_check_at: string | null;
  check_in_progress: boolean;
  source_count: number;
  projection_count: number;
  count_mismatch: boolean;
  divergent_count: number;
  missing_count: number;
  last_error: string | null;
}

export interface AiSyncMetrics {
  events_received: number;
  claims_refreshed: number;
  projections_created: number;
  projections_updated: number;
  dead_letters_created: number;
  sync_integrity_checks: number;
  sync_integrity_divergent_found: number;
}

export interface AiSyncHealth {
  status: SyncStatus;
  data_status: SyncStatus;
  worker_enabled: boolean;
  worker_status: string;
  worker_availability: 'active' | 'starting' | 'unavailable' | 'disabled' | 'error';
  last_started_at: string | null;
  last_successful_event_at: string | null;
  last_checkpoint_at: string | null;
  consecutive_error_count: number;
  sync_integrity: AiSyncIntegrity;
  metrics: AiSyncMetrics;
  last_error: string | null;
}

export interface AiDeadLetter {
  _id: string;
  claim_id: number | null;
  source_event_type: string;
  error_type: string;
  error_message: string;
  first_failed_at: string;
  last_failed_at: string;
  attempt_count: number;
  worker_version: string;
  resolved: boolean;
}

export interface AiWorkerHealth {
  status: string;
  last_started_at: string | null;
  last_completed_at: string | null;
  last_checkpoint_at: string | null;
  last_successful_event_at: string | null;
  consecutive_error_count: number;
  last_error: string | null;
}

export interface AiWorkerMetrics {
  events_received: number;
  claims_refreshed: number;
  projections_created: number;
  projections_updated: number;
  dead_letters_created: number;
  reconciliation_runs: number;
  claim_refresh_errors: number;
  claim_refresh_retries: number;
}

export interface AiWorkerStatus {
  enabled: boolean;
  health: AiWorkerHealth;
  metrics: AiWorkerMetrics;
  sync_integrity: AiSyncIntegrity;
  backfill_running?: boolean;
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

const BASE_URL = '/api/ai-analytics';

// Shared fetch wrapper bound to the AI analytics base path, with 401
// silent-refresh-retry and consistent error parsing.
const aiAnalyticsFetch = createApiFetch(BASE_URL);

function buildQueryParams(filters: AiAnalyticsFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.start_date) params.set('start_date', filters.start_date);
  if (filters.end_date) params.set('end_date', filters.end_date);
  if (filters.department_id) params.set('department_id', String(filters.department_id));
  if (filters.business_outcome) params.set('business_outcome', filters.business_outcome);
  if (filters.ai_processing_status) params.set('ai_processing_status', filters.ai_processing_status);
  if (filters.agent_execution_status) params.set('agent_execution_status', filters.agent_execution_status);
  if (filters.confidence_min !== undefined) params.set('confidence_min', String(filters.confidence_min));
  if (filters.confidence_max !== undefined) params.set('confidence_max', String(filters.confidence_max));
  if (filters.has_retry !== undefined) params.set('has_retry', String(filters.has_retry));
  if (filters.writeback_status) params.set('writeback_status', filters.writeback_status);
  if (filters.billing_category) params.set('billing_category', filters.billing_category);
  if (filters.reason_category) params.set('reason_category', filters.reason_category);
  if (filters.page) params.set('page', String(filters.page));
  if (filters.page_size) params.set('page_size', String(filters.page_size));
  if (filters.sort_by) params.set('sort_by', filters.sort_by);
  if (filters.sort_direction) params.set('sort_direction', filters.sort_direction);
  if (filters.date_basis) params.set('date_basis', filters.date_basis);
  return params;
}

export const aiAnalyticsApi = {
  getOutcomeSummary: (filters: AiAnalyticsFilters): Promise<AiOutcomeSummary> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiOutcomeSummary>(`/outcomes/summary?${params.toString()}`);
  },

  getOutcomeFunnel: (filters: AiAnalyticsFilters): Promise<AiPipelineStageStat[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiPipelineStageStat[]>(`/outcomes/funnel?${params.toString()}`);
  },

  getOutcomeTrend: (
    filters: AiAnalyticsFilters,
    grain: 'day' | 'week' | 'month' = 'day',
  ): Promise<AiOutcomeTrendPoint[]> => {
    const params = buildQueryParams(filters);
    params.set('grain', grain);
    return aiAnalyticsFetch<AiOutcomeTrendPoint[]>(`/outcomes/trend?${params.toString()}`);
  },

  getRejectionReasons: (filters: AiAnalyticsFilters): Promise<AiRejectionReasonStat[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiRejectionReasonStat[]>(`/outcomes/rejection-reasons?${params.toString()}`);
  },

  getDepartmentOutcomes: (filters: AiAnalyticsFilters): Promise<AiDepartmentOutcomeStat[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiDepartmentOutcomeStat[]>(`/outcomes/departments?${params.toString()}`);
  },

  getInvoiceCohort: (filters: AiAnalyticsFilters): Promise<AiInvoiceCohortResponse> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiInvoiceCohortResponse>(`/outcomes/invoices?${params.toString()}`);
  },

  getBillabilityStats: (filters: AiAnalyticsFilters): Promise<AiBillabilityStat> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiBillabilityStat>(`/billability/stats?${params.toString()}`);
  },

  // Diagnostics

  getDiagnosticsSummary: (filters: AiAnalyticsFilters): Promise<AiDiagnosticsSummary> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiDiagnosticsSummary>(`/diagnostics/summary?${params.toString()}`);
  },

  getStatusDistribution: (filters: AiAnalyticsFilters): Promise<AiStatusDistributionItem[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiStatusDistributionItem[]>(`/diagnostics/status?${params.toString()}`);
  },

  getConfidenceDistribution: (filters: AiAnalyticsFilters): Promise<AiConfidenceBucketStat[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiConfidenceBucketStat[]>(`/diagnostics/confidence?${params.toString()}`);
  },

  getRetryAnalysis: (filters: AiAnalyticsFilters): Promise<AiRetryAnalysis> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiRetryAnalysis>(`/diagnostics/retries?${params.toString()}`);
  },

  getWritebackAnalysis: (filters: AiAnalyticsFilters): Promise<AiWritebackAnalysis> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiWritebackAnalysis>(`/diagnostics/writeback?${params.toString()}`);
  },

  getAgentStats: (filters: AiAnalyticsFilters): Promise<AiAgentStat[]> => {
    const params = buildQueryParams(filters);
    return aiAnalyticsFetch<AiAgentStat[]>(`/diagnostics/agents?${params.toString()}`);
  },

  // Invoice trace

  getInvoiceTrace: (claimId: number): Promise<AiInvoiceTrace> => {
    return aiAnalyticsFetch<AiInvoiceTrace>(`/invoices/${claimId}/trace`);
  },

  // Sync health (Phase 11)

  getSyncHealth: (): Promise<AiSyncHealth> => {
    return aiAnalyticsFetch<AiSyncHealth>(`/worker/sync-health`);
  },

  // Dead-letters (Phase 11)

  getDeadLetters: (limit: number = 100): Promise<AiDeadLetter[]> => {
    return aiAnalyticsFetch<AiDeadLetter[]>(`/worker/dead-letters?limit=${limit}`);
  },

  resolveDeadLetter: (claimId: number): Promise<{ resolved: boolean; claim_id: number; updated: number }> => {
    return aiAnalyticsFetch(`/worker/dead-letters/${claimId}/resolve`, { method: 'POST' });
  },

  // Runtime control — start/stop the worker and trigger backfill

  startWorker: (): Promise<{ action: string; running: boolean }> => {
    return aiAnalyticsFetch('/worker/start', { method: 'POST' });
  },

  stopWorker: (): Promise<{ action: string; running: boolean }> => {
    return aiAnalyticsFetch('/worker/stop', { method: 'POST' });
  },

  triggerBackfill: (): Promise<{ action: string; backfill_running: boolean }> => {
    return aiAnalyticsFetch('/worker/backfill', { method: 'POST' });
  },

  getWorkerStatus: (): Promise<AiWorkerStatus> => {
    return aiAnalyticsFetch<AiWorkerStatus>('/worker/status');
  },
};

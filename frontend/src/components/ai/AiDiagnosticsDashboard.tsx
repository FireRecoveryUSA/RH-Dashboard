import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  CheckCircle2,
  XCircle,
  RotateCcw,
  AlertTriangle,
  Clock,
  Gauge,
  Zap,
} from 'lucide-react';
import {
  aiAnalyticsApi,
  AiAnalyticsFilters,
  AiDiagnosticsSummary,
  AiConfidenceBucketStat,
  AiAgentStat,
  AiStatusDistributionItem,
  AiRetryAnalysis,
  AiWritebackAnalysis,
} from '../../services/aiAnalyticsApi';
import { billingStyles, LoadingState, ErrorState, EmptyState, formatPercent } from '../billing/shared';
import { AiAnalyticsFilterBar } from './AiAnalyticsFilterBar';
import { useAutoRefresh } from '../../hooks/useAutoRefresh';
import { useAiDateRange } from '../../hooks/useAiDateRange';
import { SyncHealthIndicator } from './SyncHealthIndicator';
import { KpiCard, DataIncompleteBanner } from './AiDashboardPrimitives';

// ---------------------------------------------------------------------------
// Confidence calibration view
// ---------------------------------------------------------------------------

const ConfidenceCalibrationView: React.FC<{ buckets: AiConfidenceBucketStat[] }> = ({ buckets }) => {
  if (buckets.length === 0) return <EmptyState label="No confidence data available." />;
  const maxCount = Math.max(...buckets.map((b) => b.count), 1);
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Confidence Calibration
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {buckets.map((b) => (
          <div key={b.bucket} style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: '13px', fontWeight: 600 }}>
                Confidence {b.bucket}%
              </span>
              <div style={{ display: 'flex', gap: '12px', fontSize: '12px' }}>
                <span style={{ color: '#22c55e' }}>Released: {b.released}</span>
                <span style={{ color: '#ef4444' }}>Rejected: {b.rejected}</span>
                <span style={{ color: 'var(--text-muted)' }}>Pending: {b.pending}</span>
                <span style={{ fontWeight: 700 }}>
                  Release rate: {b.release_rate !== null ? formatPercent(b.release_rate) : '—'}
                </span>
              </div>
            </div>
            <div style={{ display: 'flex', height: '8px', borderRadius: '4px', overflow: 'hidden', backgroundColor: 'var(--bg-tertiary)' }}>
              {b.released > 0 && (
                <div style={{ width: `${(b.released / maxCount) * 100}%`, backgroundColor: '#22c55e' }} />
              )}
              {b.rejected > 0 && (
                <div style={{ width: `${(b.rejected / maxCount) * 100}%`, backgroundColor: '#ef4444' }} />
              )}
              {b.pending > 0 && (
                <div style={{ width: `${(b.pending / maxCount) * 100}%`, backgroundColor: '#eab308' }} />
              )}
            </div>
            <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>Total: {b.count}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Status distribution view
// ---------------------------------------------------------------------------

const StatusDistributionView: React.FC<{ items: AiStatusDistributionItem[] }> = ({ items }) => {
  if (items.length === 0) return <EmptyState label="No status data available." />;
  const grouped: Record<string, AiStatusDistributionItem[]> = {};
  for (const item of items) {
    if (!grouped[item.dimension]) grouped[item.dimension] = [];
    grouped[item.dimension].push(item);
  }
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        AI Processing Status Distribution
      </h3>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        {Object.entries(grouped).map(([dim, vals]) => {
          const maxCount = Math.max(...vals.map((v) => v.count), 1);
          return (
            <div key={dim}>
              <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
                {dim.replace(/_/g, ' ')}
              </span>
              <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {vals.map((v) => (
                  <div key={v.value} style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                      <span>{v.value}</span>
                      <span style={{ fontWeight: 600 }}>{v.count}</span>
                    </div>
                    <div style={{ height: '4px', backgroundColor: 'var(--bg-tertiary)', borderRadius: '2px', overflow: 'hidden' }}>
                      <div style={{ width: `${(v.count / maxCount) * 100}%`, height: '100%', backgroundColor: 'var(--accent-primary)' }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Retry analysis view
// ---------------------------------------------------------------------------

const RetryAnalysisView: React.FC<{ data: AiRetryAnalysis | null }> = ({ data }) => {
  if (!data || data.total_records === 0) return <EmptyState label="No retry data available." />;
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Retry Analysis
      </h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '16px', marginBottom: '20px' }}>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Total Records</span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{data.total_records}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>With Retries</span>
          <div style={{ fontSize: '24px', fontWeight: 700, color: '#eab308' }}>{data.records_with_retries}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Retry Rate</span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{formatPercent(data.retry_rate)}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Retry Success</span>
          <div style={{ fontSize: '24px', fontWeight: 700, color: '#22c55e' }}>
            {data.retry_success_rate !== null ? formatPercent(data.retry_success_rate) : '—'}
          </div>
        </div>
      </div>
      {Object.keys(data.retried_outcome_distribution).length > 0 && (
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Retried Record Outcomes
          </span>
          <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {Object.entries(data.retried_outcome_distribution).map(([outcome, count]) => (
              <div key={outcome} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span>{outcome.replace(/_/g, ' ')}</span>
                <span style={{ fontWeight: 600 }}>{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Writeback analysis view
// ---------------------------------------------------------------------------

const WritebackAnalysisView: React.FC<{ data: AiWritebackAnalysis | null }> = ({ data }) => {
  if (!data || data.total_records === 0) return <EmptyState label="No writeback data available." />;
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Writeback Status Analysis
      </h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '16px', marginBottom: '20px' }}>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Total</span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{data.total_records}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Failures</span>
          <div style={{ fontSize: '24px', fontWeight: 700, color: '#ef4444' }}>{data.failure_count}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>Failure Rate</span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{formatPercent(data.failure_rate)}</div>
        </div>
      </div>
      {Object.keys(data.status_distribution).length > 0 && (
        <div style={{ marginBottom: '16px' }}>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Status Distribution
          </span>
          <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {Object.entries(data.status_distribution).map(([status, count]) => (
              <div key={status} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span>{status.replace(/_/g, ' ')}</span>
                <span style={{ fontWeight: 600 }}>{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {Object.keys(data.failure_by_processing_status).length > 0 && (
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Failures by AI Processing Status
          </span>
          <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {Object.entries(data.failure_by_processing_status).map(([status, count]) => (
              <div key={status} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span>{status}</span>
                <span style={{ fontWeight: 600, color: '#ef4444' }}>{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Agent stats view
// ---------------------------------------------------------------------------

const AgentStatsView: React.FC<{ stats: AiAgentStat[] }> = ({ stats }) => {
  if (stats.length === 0) return <EmptyState label="No agent execution data available." />;
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Agent Execution Stats
      </h3>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['Agent', 'Status', 'Processing Stage', 'Request Type', 'Count'].map((h) => (
                <th key={h} style={{
                  fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)',
                  textTransform: 'uppercase', padding: '8px 12px', textAlign: 'left',
                  borderBottom: '1px solid var(--border-color)', whiteSpace: 'nowrap',
                }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {stats.map((s, i) => (
              <tr key={i}>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>{s.agent}</td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>{s.status}</td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>{s.processing_stage}</td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>{s.request_type}</td>
                <td style={{ padding: '8px 12px', fontSize: '13px', fontWeight: 600, borderBottom: '1px solid var(--border-color)' }}>{s.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main diagnostics dashboard
// ---------------------------------------------------------------------------

export const AiDiagnosticsDashboard: React.FC = () => {
  const { serverDate, startDate, endDate, dateReady, rangeError, setStartDate, setEndDate, defaultRangeType } = useAiDateRange();
  const [departmentId, setDepartmentId] = useState<number | undefined>(undefined);

  const [summary, setSummary] = useState<AiDiagnosticsSummary | null>(null);
  const [confidence, setConfidence] = useState<AiConfidenceBucketStat[]>([]);
  const [statusDist, setStatusDist] = useState<AiStatusDistributionItem[]>([]);
  const [retryAnalysis, setRetryAnalysis] = useState<AiRetryAnalysis | null>(null);
  const [writebackAnalysis, setWritebackAnalysis] = useState<AiWritebackAnalysis | null>(null);
  const [agentStats, setAgentStats] = useState<AiAgentStat[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-refresh every 30s so projection changes from the worker are visible
  // without a manual page reload.
  const refreshKey = useAutoRefresh(30000);

  const filters: AiAnalyticsFilters = useMemo(
    () => ({ start_date: startDate, end_date: endDate, department_id: departmentId }),
    [startDate, endDate, departmentId],
  );

  useEffect(() => {
    if (!dateReady) return;
    let active = true;
    setLoading(true);
    setError(null);

    Promise.all([
      aiAnalyticsApi.getDiagnosticsSummary(filters),
      aiAnalyticsApi.getConfidenceDistribution(filters),
      aiAnalyticsApi.getStatusDistribution(filters),
      aiAnalyticsApi.getRetryAnalysis(filters),
      aiAnalyticsApi.getWritebackAnalysis(filters),
      aiAnalyticsApi.getAgentStats(filters),
    ])
      .then(([s, c, sd, r, w, a]) => {
        if (active) {
          setSummary(s);
          setConfidence(c);
          setStatusDist(sd);
          setRetryAnalysis(r);
          setWritebackAnalysis(w);
          setAgentStats(a);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setError(err.message || 'Failed to load diagnostics data.');
          setLoading(false);
        }
      });

    return () => { active = false; };
  }, [filters, dateReady, refreshKey]);

  if (rangeError) return <ErrorState message={rangeError} />;
  if (!dateReady || (loading && !summary)) return <LoadingState label="Loading AI diagnostics…" />;
  if (error) return <ErrorState message={error} />;
  if (!summary) return null;

  return (
    <div style={billingStyles.page}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '8px' }}>
        <SyncHealthIndicator />
      </div>
      <AiAnalyticsFilterBar
        startDate={startDate}
        endDate={endDate}
        onStartDateChange={setStartDate}
        onEndDateChange={setEndDate}
        serverDate={serverDate}
        defaultRangeType={defaultRangeType}
        defaultPeriodsBack={0}
        departmentId={departmentId}
        onDepartmentIdChange={setDepartmentId}
        showOutcomeFilter={false}
      />

      {/* KPI Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
        <KpiCard label="AI Runs" value={summary.ai_runs.toLocaleString()} icon={<Activity size={20} />} />
        <KpiCard label="Did Not Qualify" value={summary.did_not_qualify.toLocaleString()} icon={<AlertTriangle size={20} />} color="#94a3b8"
          subtitle="No qualifying AI fee tile" />
        <KpiCard label="Completed" value={summary.completed.toLocaleString()} icon={<CheckCircle2 size={20} />} color="#22c55e" />
        <KpiCard label="Errors" value={summary.errors.toLocaleString()} icon={<XCircle size={20} />} color="#ef4444" />
        <KpiCard label="Retries" value={summary.retries.toLocaleString()} icon={<RotateCcw size={20} />} color="#eab308"
          subtitle={summary.retry_success > 0 ? `${summary.retry_success} succeeded` : undefined} />
        <KpiCard label="Low Confidence (<50%)" value={summary.low_confidence.toLocaleString()} icon={<AlertTriangle size={20} />} color="#eab308" />
        <KpiCard label="Writeback Failures" value={summary.writeback_failures.toLocaleString()} icon={<Zap size={20} />} color="#ef4444" />
        <KpiCard label="Avg Duration" value={summary.avg_duration !== null ? `${summary.avg_duration}s` : '—'} icon={<Clock size={20} />} />
        <KpiCard label="P95 Duration" value={summary.p95_duration !== null ? `${summary.p95_duration}s` : '—'} icon={<Gauge size={20} />}
          subtitle={summary.p50_duration !== null ? `P50: ${summary.p50_duration}s` : undefined} />
      </div>

      {/* Data completeness warning */}
      {!summary.data_complete && <DataIncompleteBanner sourceStatus={summary.source_status} />}

      {/* Confidence calibration + Status distribution */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        <ConfidenceCalibrationView buckets={confidence} />
        <StatusDistributionView items={statusDist} />
      </div>

      {/* Retry + Writeback analysis */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        <RetryAnalysisView data={retryAnalysis} />
        <WritebackAnalysisView data={writebackAnalysis} />
      </div>

      {/* Agent stats */}
      <AgentStatsView stats={agentStats} />
    </div>
  );
};

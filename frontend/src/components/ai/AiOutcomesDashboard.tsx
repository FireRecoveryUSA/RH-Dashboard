import React, { useEffect, useMemo, useState } from 'react';
import {
  TrendingUp,
  TrendingDown,
  CheckCircle2,
  XCircle,
  Clock,
  HelpCircle,
  Zap,
  AlertTriangle,
  Activity,
} from 'lucide-react';
import {
  aiAnalyticsApi,
  AiAnalyticsFilters,
  AiOutcomeSummary,
  AiPipelineStageStat,
  AiOutcomeTrendPoint,
  AiRejectionReasonStat,
  AiDepartmentOutcomeStat,
  AiBillabilityStat,
} from '../../services/aiAnalyticsApi';
import { billingStyles, LoadingState, ErrorState, EmptyState, formatPercent } from '../billing/shared';
import { AiAnalyticsFilterBar } from './AiAnalyticsFilterBar';
import { AiInvoiceCohortGrid } from './AiInvoiceCohortGrid';
import { AiInvoiceTrace } from './AiInvoiceTrace';
import { SyncHealthIndicator } from './SyncHealthIndicator';
import { useAutoRefresh } from '../../hooks/useAutoRefresh';
import { useAiDateRange } from '../../hooks/useAiDateRange';

// ---------------------------------------------------------------------------
// KPI Card
// ---------------------------------------------------------------------------

interface KpiCardProps {
  label: string;
  value: string | number;
  icon: React.ReactNode;
  color?: string;
  subtitle?: string;
}

const KpiCard: React.FC<KpiCardProps> = ({ label, value, icon, color, subtitle }) => (
  <div
    style={{
      ...billingStyles.card,
      display: 'flex',
      flexDirection: 'column',
      gap: '8px',
      minHeight: '100px',
    }}
  >
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
      <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
        {label}
      </span>
      <span style={{ color: color || 'var(--accent-primary)' }}>{icon}</span>
    </div>
    <span style={{ fontSize: '28px', fontWeight: 700, color: color || 'var(--text-primary)' }}>
      {value}
    </span>
    {subtitle && (
      <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{subtitle}</span>
    )}
  </div>
);

// ---------------------------------------------------------------------------
// Funnel visualization
// ---------------------------------------------------------------------------

const FunnelView: React.FC<{ stages: AiPipelineStageStat[] }> = ({ stages }) => {
  const maxCount = Math.max(...stages.map((s) => s.count), 1);
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        AI Invoice Pipeline Funnel
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {stages.map((stage, idx) => {
          const width = (stage.count / maxCount) * 100;
          const prevCount = idx > 0 ? stages[idx - 1].count : stage.count;
          const dropOff = idx > 0 && prevCount > 0
            ? ((prevCount - stage.count) / prevCount) * 100
            : 0;
          return (
            <div key={stage.stage} style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {stage.stage}
                </span>
                <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                  {idx > 0 && dropOff > 0 && (
                    <span style={{ fontSize: '11px', color: 'var(--color-danger)' }}>
                      -{dropOff.toFixed(1)}% drop
                    </span>
                  )}
                  <span style={{ fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)' }}>
                    {stage.count.toLocaleString()}
                  </span>
                </div>
              </div>
              <div
                style={{
                  height: '8px',
                  backgroundColor: 'var(--bg-tertiary)',
                  borderRadius: '4px',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${width}%`,
                    height: '100%',
                    backgroundColor: 'var(--accent-primary)',
                    borderRadius: '4px',
                    transition: 'width 0.3s ease',
                  }}
                />
              </div>
              {stage.description && (
                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  {stage.description}
                </span>
              )}
              {stage.breakdown && stage.breakdown.length > 0 && (
                <div
                  style={{
                    marginTop: '4px',
                    paddingLeft: '12px',
                    borderLeft: '2px solid var(--bg-tertiary)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '3px',
                  }}
                >
                  {stage.breakdown.map((b) => (
                    <div
                      key={JSON.stringify([b.label, b.match_status, b.description])}
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                      }}
                    >
                      <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                          {b.label}
                        </span>
                        {b.description && (
                          <span style={{ fontSize: '10px', color: 'var(--text-muted)', opacity: 0.8 }}>
                            {b.description}
                          </span>
                        )}
                      </div>
                      <span
                        style={{
                          fontSize: '11px',
                          fontWeight: 600,
                          color: 'var(--text-secondary, var(--text-muted))',
                        }}
                      >
                        {b.count.toLocaleString()}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Rejection reasons
// ---------------------------------------------------------------------------

const RejectionReasonsView: React.FC<{ stats: AiRejectionReasonStat[] }> = ({ stats }) => {
  if (stats.length === 0) return <EmptyState label="No rejected invoices in the selected period." />;
  const maxCount = Math.max(...stats.map((s) => s.count), 1);
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Rejection Reasons (Normalized)
      </h3>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {stats.map((stat) => (
          <div key={stat.normalized_category} style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                {stat.normalized_category.replace(/_/g, ' ')}
              </span>
              <div style={{ display: 'flex', gap: '8px' }}>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  {stat.percent.toFixed(1)}%
                </span>
                <span style={{ fontSize: '13px', fontWeight: 700, color: 'var(--text-primary)' }}>
                  {stat.count}
                </span>
              </div>
            </div>
            <div
              style={{
                height: '6px',
                backgroundColor: 'var(--bg-tertiary)',
                borderRadius: '3px',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${(stat.count / maxCount) * 100}%`,
                  height: '100%',
                  backgroundColor: '#ef4444',
                  borderRadius: '3px',
                }}
              />
            </div>
            {stat.raw_reason_breakdown.length > 1 && (
              <div style={{ paddingLeft: '12px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {stat.raw_reason_breakdown.slice(0, 5).map((r) => (
                  <span key={r.raw_reason} style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                    • {r.raw_reason}: {r.count}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Department comparison
// ---------------------------------------------------------------------------

const DepartmentView: React.FC<{ stats: AiDepartmentOutcomeStat[] }> = ({ stats }) => {
  if (stats.length === 0) return <EmptyState label="No department data in the selected period." />;
  const top10 = stats.slice(0, 10);
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Top Departments by AI Invoice Volume
      </h3>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['Department', 'State', 'Volume', 'Released', 'Rejected', 'Release Rate', 'AI Complete', 'Avg Conf.'].map((h) => (
                <th
                  key={h}
                  style={{
                    fontSize: '11px',
                    fontWeight: 600,
                    color: 'var(--text-muted)',
                    textTransform: 'uppercase',
                    padding: '8px 12px',
                    textAlign: 'left',
                    borderBottom: '1px solid var(--border-color)',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {top10.map((d) => (
              <tr key={d.department_id}>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>
                  {d.department_name || `Dept ${d.department_id}`}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', color: 'var(--text-muted)', borderBottom: '1px solid var(--border-color)' }}>
                  {d.state || '—'}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', fontWeight: 600, borderBottom: '1px solid var(--border-color)' }}>
                  {d.volume}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', color: '#22c55e', borderBottom: '1px solid var(--border-color)' }}>
                  {d.released}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', color: '#ef4444', borderBottom: '1px solid var(--border-color)' }}>
                  {d.rejected}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>
                  {d.release_rate !== null ? formatPercent(d.release_rate) : '—'}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>
                  {d.ai_completion_rate !== null ? formatPercent(d.ai_completion_rate) : '—'}
                </td>
                <td style={{ padding: '8px 12px', fontSize: '13px', borderBottom: '1px solid var(--border-color)' }}>
                  {d.avg_confidence !== null ? `${d.avg_confidence}%` : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {stats.length > 10 && (
        <div style={{ marginTop: '12px', fontSize: '12px', color: 'var(--text-muted)', textAlign: 'center' }}>
          Showing top 10 of {stats.length} departments
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Billability section (Phase 4)
// ---------------------------------------------------------------------------

const BillabilityView: React.FC<{ stats: AiBillabilityStat | null }> = ({ stats }) => {
  if (!stats) return null;
  const determinedPct = stats.ai_records > 0
    ? (stats.billability_determined / stats.ai_records) * 100
    : 0;
  return (
    <div style={billingStyles.card}>
      <h3 style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)', margin: '0 0 16px 0' }}>
        Incident / Billability Evaluation
      </h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '16px' }}>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            AI Records
          </span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{stats.ai_records}</div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Billability Determined
          </span>
          <div style={{ fontSize: '24px', fontWeight: 700, color: '#22c55e' }}>
            {stats.billability_determined}
          </div>
          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            {determinedPct.toFixed(1)}% of AI records
          </span>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Undetermined
          </span>
          <div style={{ fontSize: '24px', fontWeight: 700, color: '#eab308' }}>
            {stats.billability_undetermined}
          </div>
        </div>
        <div>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Billable
          </span>
          <div style={{ fontSize: '24px', fontWeight: 700 }}>{stats.billable}</div>
        </div>
      </div>
      {Object.keys(stats.billing_category_distribution).length > 0 && (
        <div style={{ marginTop: '20px' }}>
          <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
            Billing Category Distribution
          </span>
          <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {Object.entries(stats.billing_category_distribution).map(([cat, count]) => (
              <div key={cat} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
                <span>{cat}</span>
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
// Main dashboard
// ---------------------------------------------------------------------------

export const AiOutcomesDashboard: React.FC = () => {
  const { serverDate, startDate, endDate, dateReady, rangeError, setStartDate, setEndDate, defaultRangeType } = useAiDateRange();
  const [departmentId, setDepartmentId] = useState<number | undefined>(undefined);
  const [businessOutcome, setBusinessOutcome] = useState<string | undefined>(undefined);

  const [summary, setSummary] = useState<AiOutcomeSummary | null>(null);
  const [funnel, setFunnel] = useState<AiPipelineStageStat[]>([]);
  const [rejectionReasons, setRejectionReasons] = useState<AiRejectionReasonStat[]>([]);
  const [departments, setDepartments] = useState<AiDepartmentOutcomeStat[]>([]);
  const [billability, setBillability] = useState<AiBillabilityStat | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedClaimId, setSelectedClaimId] = useState<number | null>(null);

  // Auto-refresh every 30s so projection changes from the worker are visible
  // without a manual page reload.
  const refreshKey = useAutoRefresh(30000);

  const filters: AiAnalyticsFilters = useMemo(
    () => ({
      start_date: startDate,
      end_date: endDate,
      department_id: departmentId,
      business_outcome: businessOutcome,
    }),
    [startDate, endDate, departmentId, businessOutcome],
  );

  useEffect(() => {
    if (!dateReady) return;
    let active = true;
    setLoading(true);
    setError(null);

    Promise.all([
      aiAnalyticsApi.getOutcomeSummary(filters),
      aiAnalyticsApi.getOutcomeFunnel(filters),
      aiAnalyticsApi.getRejectionReasons(filters),
      aiAnalyticsApi.getDepartmentOutcomes(filters),
      aiAnalyticsApi.getBillabilityStats(filters),
    ])
      .then(([s, f, r, d, b]) => {
        if (active) {
          setSummary(s);
          setFunnel(f);
          setRejectionReasons(r);
          setDepartments(d);
          setBillability(b);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setError(err.message || 'Failed to load AI analytics data.');
          setLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [filters, dateReady, refreshKey]);

  if (rangeError) return <ErrorState message={rangeError} />;
  if (!dateReady || (loading && !summary)) return <LoadingState label="Loading AI outcomes data…" />;
  if (error) return <ErrorState message={error} />;
  if (!summary) return null;

  if (selectedClaimId !== null) {
    return <AiInvoiceTrace claimId={selectedClaimId} onBack={() => setSelectedClaimId(null)} />;
  }

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
        businessOutcome={businessOutcome}
        onBusinessOutcomeChange={setBusinessOutcome}
      />

      {/* KPI Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
        <KpiCard
          label="AI-Eligible Claims"
          value={summary.total_ai_invoices.toLocaleString()}
          icon={<Activity size={20} />}
          subtitle={`${summary.terminal_count.toLocaleString()} terminal`}
        />
        <KpiCard
          label="Did Not Qualify"
          value={summary.did_not_qualify.toLocaleString()}
          icon={<AlertTriangle size={20} />}
          color="#94a3b8"
          subtitle="No qualifying AI fee tile"
        />
        <KpiCard
          label="Business Release Rate"
          value={formatPercent(summary.business_release_rate)}
          icon={<TrendingUp size={20} />}
          color="#22c55e"
          subtitle={`${summary.released.toLocaleString()} released`}
        />
        <KpiCard
          label="Rejection Rate"
          value={formatPercent(summary.rejection_rate)}
          icon={<TrendingDown size={20} />}
          color="#ef4444"
          subtitle={`${summary.cancelled_rejected.toLocaleString()} rejected`}
        />
        <KpiCard
          label="Pending"
          value={summary.pending.toLocaleString()}
          icon={<Clock size={20} />}
          color="#eab308"
        />
        <KpiCard
          label="AI Completed"
          value={summary.ai_completed.toLocaleString()}
          icon={<CheckCircle2 size={20} />}
          color="#22c55e"
        />
        <KpiCard
          label="Writeback Success"
          value={summary.writeback_success.toLocaleString()}
          icon={<Zap size={20} />}
          subtitle={`${summary.writeback_failed} failed`}
        />
        <KpiCard
          label="Avg Confidence"
          value={summary.avg_confidence !== null ? `${summary.avg_confidence}%` : '—'}
          icon={<HelpCircle size={20} />}
        />
        {summary.ai_not_enabled > 0 && (
          <KpiCard
            label="Billing Not Enabled"
            value={summary.ai_not_enabled.toLocaleString()}
            icon={<AlertTriangle size={20} />}
            color="#94a3b8"
          />
        )}
      </div>

      {/* Data completeness warning */}
      {!summary.data_complete && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            padding: '12px 16px',
            backgroundColor: 'rgba(234, 179, 8, 0.1)',
            border: '1px solid rgba(234, 179, 8, 0.2)',
            borderRadius: 'var(--border-radius-md)',
            color: '#eab308',
            fontSize: '13px',
          }}
        >
          <AlertTriangle size={16} />
          <span>
            Data may be incomplete — one or more data sources were unavailable.
            Source status: {Object.entries(summary.source_status).map(([k, v]) => `${k}=${v}`).join(', ')}
          </span>
        </div>
      )}

      {/* Funnel + Rejection Reasons side by side */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
        <FunnelView stages={funnel} />
        <RejectionReasonsView stats={rejectionReasons} />
      </div>

      {/* Billability (Phase 4) */}
      <BillabilityView stats={billability} />

      {/* Department comparison */}
      <DepartmentView stats={departments} />

      {/* Invoice cohort drill-down */}
      <AiInvoiceCohortGrid filters={filters} onRowClick={(id) => setSelectedClaimId(id)} />
    </div>
  );
};

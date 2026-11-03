import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Trash2,
} from 'lucide-react';
import {
  aiAnalyticsApi,
  AiClaimComparisonResponse,
  AiClaimComparisonSchemaCheck,
  ClaimComparisonNotBillableCohort,
  ClaimComparisonSegment,
  ClaimComparisonWeek,
  ClaimComparisonWeeklyCounts,
  ClaimStatusBreakdown,
} from '../../services/aiAnalyticsApi';
import { billingStyles, LoadingState, ErrorState, EmptyState, formatPercent } from '../billing/shared';
import { SyncHealthIndicator } from './SyncHealthIndicator';
import { KpiCard, DataIncompleteBanner } from './AiDashboardPrimitives';
import { useAutoRefresh } from '../../hooks/useAutoRefresh';
import { useAiDateRange } from '../../hooks/useAiDateRange';

const BUCKET_LABELS: Record<string, string> = {
  paid: 'Paid',
  ready_to_be_paid: 'Ready to be paid',
  under_review: 'Under review',
  in_progress: 'In progress',
  denied: 'Denied',
  not_billable: 'Not billable',
};

const STATUS_ROWS: Array<keyof ClaimStatusBreakdown> = [
  'paid',
  'ready_to_be_paid',
  'under_review',
  'in_progress',
  'denied',
  'not_billable',
];

const thStyle: React.CSSProperties = {
  fontSize: '11px',
  fontWeight: 600,
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  padding: '8px 12px',
  textAlign: 'left',
  borderBottom: '1px solid var(--border-color)',
  whiteSpace: 'nowrap',
};

const tdStyle: React.CSSProperties = {
  padding: '8px 12px',
  fontSize: '13px',
  borderBottom: '1px solid var(--border-color)',
  whiteSpace: 'nowrap',
};

const sectionTitleStyle: React.CSSProperties = {
  fontSize: '15px',
  fontWeight: 700,
  color: 'var(--text-primary)',
  margin: '0 0 16px 0',
};

const pct = (value: number | null): string => (value === null ? '—' : formatPercent(value));

// Shares are over the whole displayed population: `total` covers only
// finished claims, so the other displayed buckets are added back.
const share = (count: number, b: ClaimStatusBreakdown): string => {
  const population = b.total + b.not_billable + b.under_review + b.in_progress;
  return population > 0 ? `${((count / population) * 100).toFixed(1)}%` : '—';
};

// Claims that finished the process — the rate denominator (paid + ready + denied).
const finishedCount = (b: ClaimStatusBreakdown): number =>
  b.paid + b.ready_to_be_paid + b.denied;

const SectionCard: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={billingStyles.card}>
    <h3 style={sectionTitleStyle}>{title}</h3>
    {children}
  </div>
);

// ---------------------------------------------------------------------------
// Cohort status breakdown (AI-released vs Other split into manual / ai-rejected)
// ---------------------------------------------------------------------------

const CohortStatusTable: React.FC<{ data: AiClaimComparisonResponse }> = ({ data }) => {
  const columns: Array<{ label: string; breakdown: ClaimStatusBreakdown; color?: string }> = [
    { label: 'AI-Released', breakdown: data.ai_released, color: '#22c55e' },
    { label: 'Other · Manual', breakdown: data.other_manual },
    { label: 'Other · AI-Rejected', breakdown: data.other_ai_rejected, color: '#ef4444' },
    { label: 'Other (total)', breakdown: data.other },
  ];
  return (
    <SectionCard title="Final Payment Outcome by Cohort">
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={thStyle}>Final status</th>
              {columns.map((c) => (
                <th key={c.label} style={thStyle}>{c.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {STATUS_ROWS.map((key) => (
              <tr key={key}>
                <td style={tdStyle}>{BUCKET_LABELS[key]}</td>
                {columns.map((c) => (
                  <td key={c.label} style={{ ...tdStyle, color: c.color }}>
                    {(c.breakdown[key] as number).toLocaleString()}{' '}
                    <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>
                      ({share(c.breakdown[key] as number, c.breakdown)})
                    </span>
                  </td>
                ))}
              </tr>
            ))}
            <tr>
              <td style={{ ...tdStyle, fontWeight: 700 }}>Total resolved</td>
              {columns.map((c) => (
                <td key={c.label} style={{ ...tdStyle, fontWeight: 700 }}>{c.breakdown.total.toLocaleString()}</td>
              ))}
            </tr>
            <tr>
              <td style={tdStyle}>Paid rate</td>
              {columns.map((c) => (
                <td key={c.label} style={tdStyle}>{pct(c.breakdown.paid_rate)}</td>
              ))}
            </tr>
            <tr>
              <td style={tdStyle}>Resolved success rate</td>
              {columns.map((c) => (
                <td key={c.label} style={tdStyle}>{pct(c.breakdown.resolved_success_rate)}</td>
              ))}
            </tr>
            <tr>
              <td style={tdStyle}>Denied rate</td>
              {columns.map((c) => (
                <td key={c.label} style={tdStyle}>{pct(c.breakdown.denied_rate)}</td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: '10px', fontSize: '11px', color: 'var(--text-muted)' }}>
        Totals and rates count only claims that finished the process (paid + ready to be paid + denied).
        In-progress, under-review, and not billable claims never make up a total;
        AI-pending claims ({data.ai_pending.toLocaleString()}) are excluded from all rates.
      </div>
    </SectionCard>
  );
};

// ---------------------------------------------------------------------------
// Weekly final outcomes
// ---------------------------------------------------------------------------

const WeeklyTable: React.FC<{ weeks: ClaimComparisonWeek[] }> = ({ weeks }) => {
  const cell = (c: ClaimComparisonWeeklyCounts, key: 'paid' | 'ready_to_be_paid' | 'denied') => (
    <td style={tdStyle}>{c[key].toLocaleString()}</td>
  );
  return (
    <SectionCard title="Weekly Final Outcomes (by effective date)">
      <div style={{ overflowX: 'auto', maxHeight: '420px', overflowY: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={thStyle}>Week</th>
              <th style={thStyle}>AI Paid</th>
              <th style={thStyle}>AI Ready</th>
              <th style={thStyle}>AI Denied</th>
              <th style={thStyle}>AI Success</th>
              <th style={thStyle}>Other Paid</th>
              <th style={thStyle}>Other Ready</th>
              <th style={thStyle}>Other Denied</th>
              <th style={thStyle}>Other Success</th>
            </tr>
          </thead>
          <tbody>
            {weeks.map((w) => (
              <tr key={w.week}>
                <td style={tdStyle}>
                  {w.week}{' '}
                  <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>from {w.week_start}</span>
                </td>
                {cell(w.ai_released, 'paid')}
                {cell(w.ai_released, 'ready_to_be_paid')}
                {cell(w.ai_released, 'denied')}
                <td style={tdStyle}>{pct(w.ai_released.resolved_success_rate)}</td>
                {cell(w.other, 'paid')}
                {cell(w.other, 'ready_to_be_paid')}
                {cell(w.other, 'denied')}
                <td style={tdStyle}>{pct(w.other.resolved_success_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: '10px', fontSize: '11px', color: 'var(--text-muted)' }}>
        Each claim is counted once, in the week its final status began. Other = manual + AI-rejected.
      </div>
    </SectionCard>
  );
};

// ---------------------------------------------------------------------------
// Incident outcome bar chart — paid/ready vs denied per incident type, AI vs other
// ---------------------------------------------------------------------------

const OutcomeBar: React.FC<{ breakdown: ClaimStatusBreakdown; label: string }> = ({ breakdown, label }) => {
  const successes = breakdown.paid + breakdown.ready_to_be_paid;
  const total = successes + breakdown.denied;
  const successPct = total > 0 ? (successes / total) * 100 : 0;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: '3px 0' }}>
      <span style={{ width: '52px', fontSize: '11px', color: 'var(--text-muted)', flexShrink: 0 }}>{label}</span>
      <div style={{
        flex: 1, display: 'flex', height: '18px', borderRadius: '4px', overflow: 'hidden',
        background: 'var(--bg-secondary, rgba(128,128,128,0.15))',
      }}>
        {successPct > 0 && (
          <div style={{ width: `${successPct}%`, background: '#22c55e' }} title={`Paid + ready: ${successes}`} />
        )}
        {total > 0 && successPct < 100 && (
          <div style={{ width: `${100 - successPct}%`, background: '#ef4444' }} title={`Denied: ${breakdown.denied}`} />
        )}
      </div>
      <span style={{ width: '110px', fontSize: '11px', flexShrink: 0, textAlign: 'right' }}>
        {total > 0 ? `${pct(breakdown.paid_rate)} · ${total.toLocaleString()} claims` : 'no finished claims'}
      </span>
    </div>
  );
};

const IncidentOutcomeChart: React.FC<{ segments: ClaimComparisonSegment[] }> = ({ segments }) => {
  const rows = segments.filter((s) => s.overall.total > 0);
  if (rows.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', marginBottom: '18px' }}>
      <div style={{ display: 'flex', gap: '16px', fontSize: '11px', color: 'var(--text-muted)' }}>
        <span><span style={{ display: 'inline-block', width: '10px', height: '10px', background: '#22c55e', borderRadius: '2px', marginRight: '4px' }} />Paid + ready to be paid</span>
        <span><span style={{ display: 'inline-block', width: '10px', height: '10px', background: '#ef4444', borderRadius: '2px', marginRight: '4px' }} />Denied</span>
      </div>
      {rows.map((s) => (
        <div key={s.label}>
          <div style={{ fontSize: '12px', fontWeight: 600, marginBottom: '2px' }}>{s.label}</div>
          <OutcomeBar breakdown={s.ai_released} label="AI" />
          <OutcomeBar breakdown={s.other} label="Other" />
        </div>
      ))}
      <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
        Share of finished claims (paid + ready + denied); label shows paid rate and finished count. Other = manual + AI-rejected.
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Segment tables (incident type, billing target)
// ---------------------------------------------------------------------------

const SegmentTable: React.FC<{
  title: string;
  labelHeader: string;
  segments: ClaimComparisonSegment[];
  chart?: React.ReactNode;
}> = ({ title, labelHeader, segments, chart }) => (
  <SectionCard title={title}>
    {chart}
    {segments.length === 0 ? (
      <EmptyState label="No claims in the selected period." />
    ) : (
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={thStyle}>{labelHeader}</th>
              <th style={thStyle}>AI Claims</th>
              <th style={thStyle}>AI Paid Rate</th>
              <th style={thStyle}>AI Success</th>
              <th style={thStyle}>Other Claims</th>
              <th style={thStyle}>Other Paid Rate</th>
              <th style={thStyle}>Other Success</th>
            </tr>
          </thead>
          <tbody>
            {segments.map((s) => (
              <tr key={s.label}>
                <td style={{ ...tdStyle, fontWeight: 600 }}>{s.label}</td>
                <td style={tdStyle}>{s.ai_released.total.toLocaleString()}</td>
                <td style={tdStyle}>{pct(s.ai_released.paid_rate)}</td>
                <td style={tdStyle}>{pct(s.ai_released.resolved_success_rate)}</td>
                <td style={tdStyle}>{s.other.total.toLocaleString()}</td>
                <td style={tdStyle}>{pct(s.other.paid_rate)}</td>
                <td style={tdStyle}>{pct(s.other.resolved_success_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )}
  </SectionCard>
);

// ---------------------------------------------------------------------------
// Not billable
// ---------------------------------------------------------------------------

const NotBillableCohortCard: React.FC<{ title: string; cohort: ClaimComparisonNotBillableCohort }> = ({ title, cohort }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
    <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary, var(--text-muted))' }}>
      {title}
    </span>
    <div style={{ fontSize: '13px' }}>
      Marked not billable at some point: <strong>{cohort.ever_not_billable.toLocaleString()}</strong>
    </div>
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <span style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase' }}>
        Current status of those claims
      </span>
      {Object.keys(cohort.effective_status).length === 0 && (
        <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>None</span>
      )}
      {Object.entries(cohort.effective_status).map(([bucket, count]) => (
        <div key={bucket} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '13px' }}>
          <span>{BUCKET_LABELS[bucket] ?? bucket}</span>
          <span style={{ fontWeight: 600 }}>{count.toLocaleString()}</span>
        </div>
      ))}
    </div>
    <div style={{ fontSize: '13px' }}>
      Ready or paid vs denied:{' '}
      <strong>{pct(cohort.ready_vs_denied_rate)}</strong>{' '}
      <span style={{ color: 'var(--text-muted)', fontSize: '11px' }}>
        ({cohort.ready_or_paid.toLocaleString()} ready/paid, {cohort.denied.toLocaleString()} denied)
      </span>
    </div>
  </div>
);

const NotBillableSection: React.FC<{ data: AiClaimComparisonResponse }> = ({ data }) => (
  <SectionCard title="Not Billable">
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '24px' }}>
      <NotBillableCohortCard title="AI-released runs" cohort={data.not_billable.ai_released} />
      <NotBillableCohortCard title="Other runs" cohort={data.not_billable.other} />
    </div>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '16px', marginTop: '20px' }}>
      <div>
        <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
          Marked by processor (claim status)
        </span>
        <div style={{ fontSize: '24px', fontWeight: 700 }}>
          {data.not_billable.marked_by_processor.toLocaleString()}
        </div>
      </div>
      <div>
        <span style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
          Identified by system (AI billability)
        </span>
        <div style={{ fontSize: '24px', fontWeight: 700 }}>
          {data.not_billable.identified_by_system.toLocaleString()}
        </div>
      </div>
    </div>
  </SectionCard>
);

// ---------------------------------------------------------------------------
// Data quality & schema check
// ---------------------------------------------------------------------------

const SchemaCheckPanel: React.FC<{
  schema: AiClaimComparisonSchemaCheck | null;
  error: string | null;
}> = ({ schema, error }) => {
  if (error) return <ErrorState message={error} />;
  if (!schema) return <LoadingState label="Loading schema check…" />;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div style={{ fontSize: '13px' }}>
        {Object.entries(schema.resolved_columns).map(([k, v]) => (
          <div key={k}>
            <span style={{ color: 'var(--text-muted)' }}>{k}:</span> {v ?? 'not resolved'}
          </div>
        ))}
        <div style={{ color: 'var(--text-muted)', fontSize: '12px', marginTop: '4px' }}>
          claims_deleted columns: {schema.claims_deleted_columns.join(', ') || '—'}
        </div>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={thStyle}>Claims.status (in scope)</th>
              <th style={thStyle}>Claims</th>
              <th style={thStyle}>Bucket</th>
              <th style={thStyle}>Mapped</th>
            </tr>
          </thead>
          <tbody>
            {schema.status_values.map((s) => (
              <tr key={String(s.status)}>
                <td style={tdStyle}>{s.status ?? '(null)'}</td>
                <td style={tdStyle}>{s.claim_count.toLocaleString()}</td>
                <td style={tdStyle}>{BUCKET_LABELS[s.bucket] ?? s.bucket}</td>
                <td style={{ ...tdStyle, color: s.mapped ? undefined : '#eab308' }}>
                  {s.mapped ? 'yes' : 'no — counted as In progress'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={thStyle}>Run type (Claims.type_id)</th>
              <th style={thStyle}>Runs (in scope)</th>
              <th style={thStyle}>Incident type label</th>
            </tr>
          </thead>
          <tbody>
            {schema.run_type_values.map((r) => (
              <tr key={String(r.type_id)}>
                <td style={tdStyle}>{r.type_id ?? '(null)'}</td>
                <td style={tdStyle}>{r.claim_count.toLocaleString()}</td>
                <td style={tdStyle}>{r.label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main dashboard
// ---------------------------------------------------------------------------

export const AiClaimComparisonDashboard: React.FC = () => {
  const { dateReady, rangeError } = useAiDateRange('year');
  const [data, setData] = useState<AiClaimComparisonResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [schemaOpen, setSchemaOpen] = useState(false);
  const [schema, setSchema] = useState<AiClaimComparisonSchemaCheck | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // Claim outcomes move slowly; a 30s poll matches the other AI dashboards.
  const refreshKey = useAutoRefresh(30000);

  useEffect(() => {
    if (!dateReady) return;
    let active = true;
    setLoading(true);
    setError(null);

    aiAnalyticsApi.getClaimComparison()
      .then((result) => {
        if (active) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setError(err.message || 'Failed to load claim payment comparison.');
          setLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [dateReady, refreshKey]);

  useEffect(() => {
    if (!schemaOpen || schema || schemaError) return;
    let active = true;
    aiAnalyticsApi.getClaimComparisonSchemaCheck()
      .then((result) => {
        if (active) setSchema(result);
      })
      .catch((err) => {
        if (active) setSchemaError(err.message || 'Failed to load schema check.');
      });
    return () => {
      active = false;
    };
  }, [schemaOpen, schema, schemaError]);

  const unmappedEntries = useMemo(
    () => Object.entries(data?.unmapped_statuses ?? {}),
    [data],
  );

  if (rangeError) return <ErrorState message={rangeError} />;
  if (!dateReady || (loading && !data)) return <LoadingState label="Loading claim payment comparison…" />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  return (
    <div style={billingStyles.page}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
          Period (from Jul 1): {data.period_start} → {data.period_end}. Final payment result per run; Rural Metro
          departments excluded.
        </span>
        <SyncHealthIndicator />
      </div>

      {!data.data_complete && <DataIncompleteBanner sourceStatus={data.source_status} />}

      {unmappedEntries.length > 0 && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '12px',
            padding: '12px 16px',
            backgroundColor: 'rgba(234, 179, 8, 0.1)',
            border: '1px solid rgba(234, 179, 8, 0.2)',
            borderRadius: 'var(--border-radius-md)',
            color: '#eab308',
            fontSize: '13px',
          }}
        >
          <span>
            {unmappedEntries.length} claim status value(s) are not mapped and count as In progress:{' '}
            {unmappedEntries.map(([s, n]) => `${s} (${n})`).join(', ')}
          </span>
          <button
            type="button"
            onClick={() => {
              setSchemaError(null);
              setSchemaOpen(true);
            }}
            style={{ background: 'none', border: '1px solid currentColor', borderRadius: '4px', padding: '4px 8px', color: 'inherit', cursor: 'pointer' }}
          >
            Open schema check
          </button>
        </div>
      )}

      {/* KPI Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
        <KpiCard
          label="Overall Paid Rate"
          value={pct(data.overall.paid_rate)}
          icon={<Activity size={20} />}
          subtitle={`${(data.overall.paid + data.overall.ready_to_be_paid).toLocaleString()} paid-ready of ${finishedCount(data.overall).toLocaleString()} finished`}
        />
        <KpiCard
          label="AI-Released Paid Rate"
          value={pct(data.ai_released.paid_rate)}
          icon={<CheckCircle2 size={20} />}
          color="#22c55e"
          subtitle={`Success ${pct(data.ai_released.resolved_success_rate)}`}
        />
        <KpiCard
          label="Other Paid Rate"
          value={pct(data.other.paid_rate)}
          icon={<Activity size={20} />}
          color="#94a3b8"
          subtitle={`Success ${pct(data.other.resolved_success_rate)}`}
        />
        <KpiCard
          label="Claims Compared"
          value={data.overall.total.toLocaleString()}
          icon={<Activity size={20} />}
          subtitle={`${data.ai_released.total.toLocaleString()} AI-released · ${data.other.total.toLocaleString()} other`}
        />
        <KpiCard
          label="Deleted Runs"
          value={data.deleted_runs.toLocaleString()}
          icon={<Trash2 size={20} />}
          color="#94a3b8"
          subtitle="Shown only; not in rates"
        />
        <KpiCard
          label="AI Pending (excluded)"
          value={data.ai_pending.toLocaleString()}
          icon={<Clock size={20} />}
          color="#eab308"
          subtitle="AI grid outcome not final"
        />
      </div>

      <CohortStatusTable data={data} />
      <WeeklyTable weeks={data.weekly} />
      <SegmentTable
        title="By Incident Type"
        labelHeader="Incident type"
        segments={data.by_incident_type}
        chart={<IncidentOutcomeChart segments={data.by_incident_type} />}
      />
      <SegmentTable title="By Billing Target" labelHeader="Billing target" segments={data.by_billing_target} />
      <NotBillableSection data={data} />

      <SectionCard title="Data Quality & Schema Check">
        <button
          type="button"
          onClick={() => {
            setSchemaOpen((open) => {
              // Reopening after a failed fetch clears the error so the
              // effect retries instead of staying stuck on the error state.
              if (!open) setSchemaError(null);
              return !open;
            });
          }}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            background: 'none',
            border: 'none',
            color: 'var(--accent-primary)',
            cursor: 'pointer',
            fontSize: '13px',
            fontWeight: 600,
            padding: 0,
          }}
        >
          {schemaOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          {schemaOpen ? 'Hide schema check' : 'Show schema check'}
        </button>
        {schemaOpen && (
          <div style={{ marginTop: '16px' }}>
            <SchemaCheckPanel schema={schema} error={schemaError} />
          </div>
        )}
        {schemaOpen && schema && (
          <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-muted)' }}>
            <AlertTriangle size={12} style={{ verticalAlign: 'middle' }} /> Status buckets and billing labels
            are provisional until confirmed here. Edit backend/ai_analytics/claim_status_config.py.
          </div>
        )}
      </SectionCard>
    </div>
  );
};

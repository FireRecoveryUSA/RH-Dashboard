import React from 'react';
import { AlertTriangle } from 'lucide-react';
import { billingStyles } from '../billing/shared';

// ---------------------------------------------------------------------------
// KPI Card — shared by the AI dashboards
// ---------------------------------------------------------------------------

interface KpiCardProps {
  label: string;
  value: string | number;
  icon: React.ReactNode;
  color?: string;
  subtitle?: string;
}

export const KpiCard: React.FC<KpiCardProps> = ({ label, value, icon, color, subtitle }) => (
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
// Data completeness banner — shared by the AI dashboards
// ---------------------------------------------------------------------------

export const DataIncompleteBanner: React.FC<{ sourceStatus: Record<string, string> }> = ({ sourceStatus }) => (
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
      Source status: {Object.entries(sourceStatus).map(([k, v]) => `${k}=${v}`).join(', ')}
    </span>
  </div>
);

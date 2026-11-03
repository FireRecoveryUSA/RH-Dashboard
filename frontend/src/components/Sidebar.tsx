import React, { useState, useEffect } from 'react';
import { useAuth } from './AuthContext';
import { 
  LayoutDashboard, 
  Plus, 
  LogOut, 
  User, 
  ChevronRight,
  ChevronDown,
  Database,
  DollarSign,
  BarChart3,
  TrendingUp,
  Target,
  Lightbulb,
  FileText,
  Clock,
  Sparkles,
  Zap,
  Activity,
  LucideIcon,
  GitCompareArrows,
} from 'lucide-react';
import { Dashboard } from '../services/api';
import { BillingView } from './billing/types';
import { WorkerToggle } from './ai/WorkerToggle';

export type AiAnalyticsView = 'adoption' | 'outcomes' | 'claim-comparison' | 'diagnostics';

interface BillingSidebarItem {
  id: BillingView;
  label: string;
  icon: LucideIcon;
}

const BILLING_NAV_ITEMS: BillingSidebarItem[] = [
  { id: 'billing-overview',     label: 'Cost Overview',   icon: BarChart3 },
  { id: 'billing-top-spenders', label: 'Top Spenders',    icon: TrendingUp },
  { id: 'billing-budgets',      label: 'Budgets & Alerts', icon: Target },
  { id: 'billing-advisor',      label: 'Advisor',         icon: Lightbulb },
  { id: 'billing-invoices',     label: 'Invoices',        icon: FileText },
  { id: 'billing-reservations', label: 'Reservations',    icon: Clock },
  { id: 'billing-ai',           label: 'AI Cost Analyst', icon: Sparkles },
];

interface AiAnalyticsNavItem {
  id: AiAnalyticsView;
  label: string;
  icon: LucideIcon;
}

const AI_ANALYTICS_NAV_ITEMS: AiAnalyticsNavItem[] = [
  { id: 'adoption',    label: 'AI Adoption',     icon: Zap },
  { id: 'outcomes',    label: 'AI Outcomes',     icon: TrendingUp },
  { id: 'claim-comparison', label: 'Claim Payment Comparison', icon: GitCompareArrows },
  { id: 'diagnostics', label: 'AI Diagnostics',  icon: Activity },
];

interface SidebarProps {
  dashboards: Dashboard[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  isDesignerOpen: boolean;
  activeBillingView?: BillingView | null;
  onSelectBillingView?: (view: BillingView) => void;
  activeAiAnalyticsView?: AiAnalyticsView | null;
  onSelectAiAnalyticsView?: (view: AiAnalyticsView) => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  dashboards,
  selectedId,
  onSelect,
  onNew,
  isDesignerOpen,
  activeBillingView = null,
  onSelectBillingView,
  activeAiAnalyticsView = null,
  onSelectAiAnalyticsView,
}) => {
  const [billingExpanded, setBillingExpanded] = useState(false);
  // AI Analytics folder auto-expands when one of its views is active,
  // and collapses when the user navigates away to a dashboard.
  const [aiAnalyticsExpanded, setAiAnalyticsExpanded] = useState(activeAiAnalyticsView !== null);
  useEffect(() => {
    setAiAnalyticsExpanded(activeAiAnalyticsView !== null);
  }, [activeAiAnalyticsView]);
  const { user, logout } = useAuth();

  const staticDashboards = dashboards.filter((dashboard, index, all) => (
    dashboard.created_by === 'system'
      && all.findIndex((candidate) => candidate.name === dashboard.name && candidate.created_by === 'system') === index
  ));
  const savedDashboards = dashboards.filter((dashboard) => dashboard.created_by !== 'system');

  // Get initials for profile avatar
  const getInitials = (name: string) => {
    return name
      .split(' ')
      .map((n) => n[0])
      .join('')
      .toUpperCase()
      .substring(0, 2);
  };

  return (
    <aside className="sidebar" data-testid="sidebar">
      <div className="sidebar-header">
        <Database size={24} className="trend-up" style={{ color: 'var(--accent-primary)' }} />
        <span className="sidebar-logo">RecoveryHub</span>
      </div>

      <nav className="sidebar-menu">
        <div className="sidebar-menu-section">Main Menu</div>
        
        <button 
          className={`sidebar-item ${isDesignerOpen ? 'active' : ''}`}
          onClick={onNew}
          style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left' }}
        >
          <Plus size={18} />
          <span>New Dashboard</span>
        </button>

        <button
          className="sidebar-item"
          onClick={() => setAiAnalyticsExpanded((v) => !v)}
          style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left', marginTop: '8px' }}
        >
          <Sparkles size={18} style={{ color: 'var(--accent-primary)' }} />
          <span style={{ flex: 1, fontWeight: 600 }}>AI Analytics</span>
          {aiAnalyticsExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>

        {aiAnalyticsExpanded && (
          AI_ANALYTICS_NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const isActive = activeAiAnalyticsView === item.id;
            return (
              <button
                key={item.id}
                className={`sidebar-item ${isActive ? 'active' : ''}`}
                onClick={() => onSelectAiAnalyticsView?.(item.id)}
                style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left', paddingLeft: '24px' }}
              >
                <Icon size={16} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {item.label}
                </span>
              </button>
            );
          })
        )}

        {staticDashboards.map((dash) => {
          const id = dash.id || dash._id || '';
          const isActive = !isDesignerOpen && !activeAiAnalyticsView && !activeBillingView && selectedId === id;
          return (
            <button
              key={id}
              className={`sidebar-item ${isActive ? 'active' : ''}`}
              onClick={() => onSelect(id)}
              style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left' }}
            >
              <LayoutDashboard size={18} />
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {dash.name}
              </span>
              <ChevronRight size={14} style={{ opacity: isActive ? 1 : 0.3 }} />
            </button>
          );
        })}

        <button
          className="sidebar-item"
          onClick={() => setBillingExpanded((v) => !v)}
          style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left', marginTop: '8px' }}
        >
          <DollarSign size={18} style={{ color: 'var(--accent-primary)' }} />
          <span style={{ flex: 1, fontWeight: 600 }}>Azure Billing</span>
          {billingExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>

        {billingExpanded &&
          BILLING_NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const isActive = activeBillingView === item.id;
            return (
              <button
                key={item.id}
                className={`sidebar-item ${isActive ? 'active' : ''}`}
                onClick={() => onSelectBillingView?.(item.id)}
                style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left', paddingLeft: '24px' }}
              >
                <Icon size={16} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {item.label}
                </span>
              </button>
            );
          })}

        <div style={{ margin: '16px 0 8px 0' }} className="sidebar-menu-section">
          Saved Dashboards
        </div>

        {savedDashboards.length === 0 ? (
          <div style={{ padding: '8px 12px', fontSize: '12px', color: 'var(--text-muted)' }}>
            No dashboards created.
          </div>
        ) : (
          savedDashboards.map((dash) => {
            const id = dash.id || dash._id || '';
            const isActive = !isDesignerOpen && !activeAiAnalyticsView && !activeBillingView && selectedId === id;
            return (
              <button
                key={id}
                className={`sidebar-item ${isActive ? 'active' : ''}`}
                onClick={() => onSelect(id)}
                style={{ width: '100%', border: 'none', background: 'none', textAlign: 'left' }}
              >
                <LayoutDashboard size={18} />
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {dash.name}
                </span>
                <ChevronRight size={14} style={{ opacity: isActive ? 1 : 0.3 }} />
              </button>
            );
          })
        )}
      </nav>

      <WorkerToggle />

      {user && (
        <div className="sidebar-footer">
          <div className="user-profile">
            <div className="avatar">
              {getInitials(user.name)}
            </div>
            <div className="user-info">
              <span className="user-name">{user.name}</span>
              <span className="user-email">{user.email}</span>
            </div>
            <button
              onClick={logout}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--text-muted)',
                cursor: 'pointer',
                marginLeft: 'auto',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '4px',
                borderRadius: '4px'
              }}
              title="Logout"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      )}
    </aside>
  );
};

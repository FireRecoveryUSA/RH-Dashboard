import React, { useEffect, useState } from 'react';
import { useAuth } from './components/AuthContext';
import { Sidebar } from './components/Sidebar';
import { Navbar } from './components/Navbar';
import { DashboardViewer } from './components/DashboardViewer';
import { DashboardDesigner } from './components/DashboardDesigner';
import { Dashboard, api } from './services/api';
import { BillingView } from './components/billing/types';
import { BillingOverview } from './components/billing/BillingOverview';
import { TopSpendersTable } from './components/billing/TopSpendersTable';
import { BudgetsPanel } from './components/billing/BudgetsPanel';
import { AdvisorPanel } from './components/billing/AdvisorPanel';
import { InvoiceList } from './components/billing/InvoiceList';
import { ReservationDashboard } from './components/billing/ReservationDashboard';
import { AICostAnalyst } from './components/billing/AICostAnalyst';
import { AiAdoptionDashboard } from './components/ai/AiAdoptionDashboard';
import { AiOutcomesDashboard } from './components/ai/AiOutcomesDashboard';
import { AiClaimComparisonDashboard } from './components/ai/AiClaimComparisonDashboard';
import { AiDiagnosticsDashboard } from './components/ai/AiDiagnosticsDashboard';
import { AiAnalyticsView } from './components/Sidebar';
import { 
  Database, 
  ShieldAlert, 
  Loader2, 
  AlertCircle,
  FileSpreadsheet
} from 'lucide-react';

const BILLING_VIEW_TITLES: Record<BillingView, { title: string; description: string }> = {
  'billing-overview':     { title: 'Cost Overview',    description: 'Month-to-date Azure spend, top services, and budget health' },
  'billing-top-spenders': { title: 'Top Spenders',     description: 'Highest-cost services and resource groups this period' },
  'billing-budgets':      { title: 'Budgets & Alerts', description: 'Budget utilization and active cost alerts' },
  'billing-advisor':      { title: 'Azure Advisor',    description: 'Cost, security, and performance recommendations' },
  'billing-invoices':     { title: 'Invoices',         description: 'Billing invoices and downloadable statements' },
  'billing-reservations': { title: 'Reservations',     description: 'Reserved instance purchase opportunities' },
  'billing-ai':           { title: 'AI Cost Analyst',  description: 'Ask natural language questions about your Azure costs' },
};

export const AppContent: React.FC = () => {
  const { isAuthenticated, loading: authLoading, login, user } = useAuth();
  
  // Dashboard states
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dashboardsLoading, setDashboardsLoading] = useState(false);
  const [dashboardsError, setDashboardsError] = useState<string | null>(null);

  // Designer state
  const [isDesignerOpen, setIsDesignerOpen] = useState(false);
  const [editDashboard, setEditDashboard] = useState<Dashboard | null>(null);

  // Azure billing view state
  const [activeBillingView, setActiveBillingView] = useState<BillingView | null>(null);
  // AI Analytics view state — defaults to 'adoption' so AI Adoption is the landing view
  const [activeAiAnalyticsView, setActiveAiAnalyticsView] = useState<AiAnalyticsView | null>('adoption');

  // Fetch dashboards on login
  useEffect(() => {
    if (!isAuthenticated) return;

    let active = true;
    setDashboardsLoading(true);
    setDashboardsError(null);

    api.getDashboards()
      .then((data) => {
        if (active) {
          setDashboards(data);
          setDashboardsLoading(false);
          // Auto-select first dashboard if available
          if (data.length > 0) {
            const firstId = data[0].id || data[0]._id || null;
            setSelectedId(firstId);
          }
        }
      })
      .catch((err) => {
        if (active) {
          console.error("Failed to load dashboards list:", err);
          setDashboardsError(err.message || "Failed to load dashboards from metadata store.");
          setDashboardsLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [isAuthenticated]);

  const loadDashboards = async () => {
    setDashboardsLoading(true);
    setDashboardsError(null);
    try {
      const data = await api.getDashboards();
      setDashboards(data);
      setDashboardsLoading(false);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to refresh list.";
      setDashboardsError(msg);
      setDashboardsLoading(false);
    }
  };

  const handleSelectDashboard = (id: string) => {
    setIsDesignerOpen(false);
    setEditDashboard(null);
    setActiveBillingView(null);
    setActiveAiAnalyticsView(null);
    setSelectedId(id);
  };

  const handleSelectBillingView = (view: BillingView) => {
    setIsDesignerOpen(false);
    setEditDashboard(null);
    setActiveAiAnalyticsView(null);
    setActiveBillingView(view);
  };

  const handleNewDashboard = () => {
    setEditDashboard(null);
    setActiveBillingView(null);
    setActiveAiAnalyticsView(null);
    setIsDesignerOpen(true);
  };

  const handleEditDashboard = () => {
    const current = dashboards.find(d => (d.id || d._id) === selectedId);
    if (current) {
      setEditDashboard(current);
      setActiveAiAnalyticsView(null);
      setIsDesignerOpen(true);
    }
  };

  const handleSelectAiAnalyticsView = (view: AiAnalyticsView) => {
    setIsDesignerOpen(false);
    setEditDashboard(null);
    setActiveBillingView(null);
    setSelectedId(null);
    setActiveAiAnalyticsView(view);
  };

  const handleDeleteDashboard = async () => {
    if (!selectedId) return;
    if (!window.confirm("Are you sure you want to delete this dashboard? This cannot be undone.")) return;

    try {
      await api.deleteDashboard(selectedId);
      // Reload and clear selections
      const updated = dashboards.filter(d => (d.id || d._id) !== selectedId);
      setDashboards(updated);
      setSelectedId(updated.length > 0 ? (updated[0].id || updated[0]._id || null) : null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to delete.";
      alert(`Delete failed: ${msg}`);
    }
  };

  // --- LOADING / ERROR STATES ---

  if (authLoading) {
    return (
      <div 
        style={{
          height: '100vh',
          width: '100vw',
          backgroundColor: 'var(--bg-primary)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '16px'
        }}
      >
        <Loader2 size={36} className="loader" style={{ color: 'var(--accent-primary)' }} />
        <span style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>Authenticating user session...</span>
      </div>
    );
  }

  // 1. UNAUTHENTICATED SIGN-IN SCREEN
  if (!isAuthenticated) {
    return (
      <div 
        style={{
          height: '100vh',
          width: '100vw',
          backgroundColor: '#090d16',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '24px',
          fontFamily: "'Inter', sans-serif"
        }}
      >
        <div 
          style={{
            maxWidth: '440px',
            width: '100%',
            backgroundColor: 'var(--bg-secondary)',
            border: '1px solid var(--border-color)',
            borderRadius: '16px',
            padding: '40px',
            boxShadow: '0 15px 35px rgba(0,0,0,0.6)',
            textAlign: 'center',
            display: 'flex',
            flexDirection: 'column',
            gap: '24px'
          }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px' }}>
            <div 
              style={{
                width: '60px',
                height: '60px',
                borderRadius: '12px',
                backgroundColor: 'rgba(99, 102, 241, 0.1)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: 'var(--accent-primary)'
              }}
            >
              <Database size={32} />
            </div>
            <h1 style={{ color: 'white', fontSize: '24px', fontWeight: 700, letterSpacing: '-0.02em', marginTop: '12px' }}>
              RecoveryHub Portal
            </h1>
            <p style={{ color: 'var(--text-secondary)', fontSize: '14px', lineHeight: '1.5' }}>
              Monitor emergency fire run logs, track claim statuses, and view invoicing flows across departments.
            </p>
          </div>

          <button 
            className="btn btn-primary" 
            onClick={login}
            style={{
              padding: '12px 24px',
              fontSize: '15px',
              fontWeight: 600,
              boxShadow: '0 4px 12px rgba(99, 102, 241, 0.25)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '12px'
            }}
          >
            <ShieldAlert size={18} />
            <span>Sign In with Microsoft</span>
          </button>

          <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            Requires corporate login credentials matching your organization's streamlineas.com domain directory.
          </div>
        </div>
      </div>
    );
  }

  // 2. AUTHENTICATED WORKSPACE PORTAL
  const selectedDashboard = dashboards.find(d => (d.id || d._id) === selectedId);

  const renderBillingView = () => {
    switch (activeBillingView) {
      case 'billing-overview':     return <BillingOverview />;
      case 'billing-top-spenders': return <TopSpendersTable />;
      case 'billing-budgets':      return <BudgetsPanel />;
      case 'billing-advisor':      return <AdvisorPanel />;
      case 'billing-invoices':     return <InvoiceList />;
      case 'billing-reservations': return <ReservationDashboard />;
      case 'billing-ai':           return <AICostAnalyst />;
      default:                     return null;
    }
  };

  const AI_ANALYTICS_TITLES: Record<AiAnalyticsView, { title: string; description: string }> = {
    adoption: {
      title: 'AI Adoption',
      description: 'Rank fire departments by draft activity and identify the highest-volume non-AI onboarding opportunities.',
    },
    outcomes: {
      title: 'AI Outcomes & Success',
      description: 'Track AI invoice lifecycle outcomes, release rates, rejection reasons, and billability evaluation.',
    },
    'claim-comparison': {
      title: 'Claim Payment Comparison',
      description: 'Compare final payment results of AI-released runs against all other runs, by week, incident type, billing target, and not-billable status.',
    },
    diagnostics: {
      title: 'AI Diagnostics',
      description: 'Monitor AI execution health, confidence calibration, retry patterns, writeback failures, and agent stats.',
    },
  };

  const title = activeAiAnalyticsView
    ? AI_ANALYTICS_TITLES[activeAiAnalyticsView].title
    : activeBillingView
      ? BILLING_VIEW_TITLES[activeBillingView].title
      : isDesignerOpen
        ? (editDashboard ? `Editing: ${editDashboard.name}` : "Create New Dashboard")
        : (selectedDashboard ? selectedDashboard.name : "No Dashboard Selected");

  const description = activeAiAnalyticsView
    ? AI_ANALYTICS_TITLES[activeAiAnalyticsView].description
    : activeBillingView
      ? BILLING_VIEW_TITLES[activeBillingView].description
      : isDesignerOpen
        ? "Design widgets using SQL queries or embed external reporting widgets"
        : (selectedDashboard?.description || "Select a dashboard or create one to start mapping claims logs");

  return (
    <div className="app-container">
      {/* Sidebar navigation */}
      <Sidebar
        dashboards={dashboards}
        selectedId={selectedId}
        onSelect={handleSelectDashboard}
        onNew={handleNewDashboard}
        isDesignerOpen={isDesignerOpen}
        activeBillingView={activeBillingView}
        onSelectBillingView={handleSelectBillingView}
        activeAiAnalyticsView={activeAiAnalyticsView}
        onSelectAiAnalyticsView={handleSelectAiAnalyticsView}
      />

      {/* Main app body area */}
      <div className="main-content">
        
        {/* Top Navbar */}
        <Navbar
          title={title}
          titleNote={activeAiAnalyticsView ? 'excluding rural metro departments' : undefined}
          description={description}
          isConnecting={dashboardsLoading}
          onRefresh={!isDesignerOpen && !activeBillingView && !activeAiAnalyticsView ? loadDashboards : undefined}
          dbStatus={dashboardsError ? 'error' : 'connected'}
        />

        {/* Dynamic Inner Body */}
        <main className="page-body">
          {dashboardsError && (
            <div 
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '16px 24px',
                backgroundColor: 'rgba(239, 68, 68, 0.1)',
                border: '1px solid rgba(239, 68, 68, 0.2)',
                borderRadius: '8px',
                color: 'var(--color-danger)',
                fontSize: '14px',
                fontWeight: 500
              }}
            >
              <AlertCircle size={18} />
              <span><strong>Metadata Fetch Error:</strong> {dashboardsError}</span>
            </div>
          )}

          {activeAiAnalyticsView === 'adoption' ? (
            <AiAdoptionDashboard />
          ) : activeAiAnalyticsView === 'outcomes' ? (
            <AiOutcomesDashboard />
          ) : activeAiAnalyticsView === 'claim-comparison' ? (
            <AiClaimComparisonDashboard />
          ) : activeAiAnalyticsView === 'diagnostics' ? (
            <AiDiagnosticsDashboard />
          ) : activeBillingView ? (
            renderBillingView()
          ) : isDesignerOpen ? (
            <DashboardDesigner
              initialDashboard={editDashboard}
              onSave={async () => {
                setIsDesignerOpen(false);
                setEditDashboard(null);
                await loadDashboards();
              }}
              onCancel={() => {
                setIsDesignerOpen(false);
                setEditDashboard(null);
              }}
            />
          ) : selectedDashboard ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
              
              {/* Dashboard operational buttons (edit / delete) */}
              {selectedDashboard.created_by !== 'system' && (
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                  <button className="btn" onClick={handleEditDashboard} style={{ padding: '8px 14px', fontSize: '13px' }}>
                    <span>Edit Layout</span>
                  </button>
                  <button className="btn btn-danger" onClick={handleDeleteDashboard} style={{ padding: '8px 14px', fontSize: '13px' }}>
                    <span>Delete Dashboard</span>
                  </button>
                </div>
              )}

              {/* Renders widgets and grid */}
              <DashboardViewer dashboard={selectedDashboard} />
            </div>
          ) : (
            !dashboardsLoading && (
              <div 
                className="card" 
                style={{ 
                  height: '400px', 
                  justifyContent: 'center', 
                  alignItems: 'center', 
                  color: 'var(--text-secondary)',
                  gap: '12px',
                  borderStyle: 'dashed'
                }}
              >
                <FileSpreadsheet size={40} style={{ color: 'var(--text-muted)' }} />
                <span style={{ fontSize: '15px', fontWeight: 600 }}>Get Started with RecoveryHub</span>
                <p style={{ fontSize: '13px', color: 'var(--text-muted)', textAlign: 'center', maxWidth: '300px' }}>
                  There are no dashboards defined in this environment yet. Click the button below to add your first operational grid.
                </p>
                <button className="btn btn-primary" onClick={handleNewDashboard} style={{ marginTop: '8px' }}>
                  <span>Create Dashboard</span>
                </button>
              </div>
            )
          )}
        </main>
      </div>
    </div>
  );
};

export const App: React.FC = () => {
  return <AppContent />;
};

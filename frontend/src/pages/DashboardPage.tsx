/**
 * Dashboard 仪表盘
 */
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, Clock, TrendingUp, TrendingDown,
  Activity, FileText, ShieldCheck, Database,
} from 'lucide-react';
import { caseApi } from '@/services/case.service';
import { auditApi } from '@/services/audit.service';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import {
  formatDateTime, severityBadgeClass, severityLabel,
  sourceBadgeClass, statusBadgeClass, statusLabel, timeAgo,
} from '@/lib/utils';
import type { KPI } from '@/types';

export function DashboardPage() {
  const { data: kpi, isLoading: kpiLoading, } = useQuery<KPI>({
    queryKey: ['kpi'],
    queryFn: caseApi.getKpi,
  });

  const { data: casesData, isLoading: casesLoading, isError: casesError, refetch: refetchCases } = useQuery({
    queryKey: ['cases', { page: 1, page_size: 5 }],
    queryFn: () => caseApi.list({ page: 1, page_size: 5 }),
  });

  const { data: auditData, isError: auditError } = useQuery({
    queryKey: ['audit-recent', { page: 1, page_size: 5 }],
    queryFn: () => auditApi.list({ page: 1, page_size: 5 }),
  });

  const { data: sources, isError: sourcesError } = useQuery({
    queryKey: ['source-systems'],
    queryFn: caseApi.getSourceSystems,
  });

  // 真实健康态：任一数据源加载失败即降级显示（替代硬编码「系统正常」）
  const degraded = casesError || auditError || sourcesError;

  return (
    <div className="p-6 space-y-6">
      {/* 标题区 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-ink-900">仪表盘</h1>
          <p className="text-sm text-ink-500 mt-1">偏差调查全局视图与关键指标</p>
        </div>
        <Badge variant={degraded ? 'warning' : 'success'}>
          <ShieldCheck className="w-3 h-3" /> {degraded ? '部分数据源异常' : '系统正常'}
        </Badge>
      </div>

      {/* KPI 卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          title="未解决偏差"
          value={kpi?.unsolved_count ?? '—'}
          trend={kpi?.trend_unsolved_delta}
          icon={AlertTriangle}
          color="danger"
          loading={kpiLoading}
        />
        <KpiCard
          title="本月重大偏差"
          value={kpi?.major_count_this_month ?? '—'}
          trend={kpi?.trend_major_delta}
          icon={Activity}
          color="warning"
          loading={kpiLoading}
        />
        <KpiCard
          title="平均闭环天数"
          value={kpi?.avg_cycle_days != null ? `${kpi.avg_cycle_days}d` : '—'}
          trend={kpi?.trend_cycle_delta}
          icon={Clock}
          color="brand"
          loading={kpiLoading}
          invertTrend
        />
        <KpiCard
          title="CAPA 逾期率"
          value={kpi?.capa_overdue_rate != null ? `${(kpi.capa_overdue_rate * 100).toFixed(1)}%` : '—'}
          trend={kpi?.trend_capa_delta}
          icon={FileText}
          color="success"
          loading={kpiLoading}
          invertTrend
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 最近案例 */}
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>最近偏差案例</CardTitle>
            <Link to="/inbox" className="text-xs text-brand-600 hover:underline">
              查看全部 →
            </Link>
          </CardHeader>
          <CardBody className="p-0">
            <div className="divide-y divide-ink-200">
              {casesError ? (
                <ErrorBanner message="案例列表加载失败" onRetry={() => refetchCases()} />
              ) : casesLoading ? (
                <div className="px-5 py-8 text-center text-sm text-ink-500">加载中...</div>
              ) : !casesData?.items?.length ? (
                <div className="px-5 py-8 text-center text-sm text-ink-500">
                  暂无案例 — 点击右上角「查看全部」创建第一条偏差
                </div>
              ) : (
                casesData.items.map((c) => (
                <Link
                  key={c.id}
                  to={`/investigation/${c.id}`}
                  className="block px-5 py-3 hover:bg-ink-50 transition-colors card-hover"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-mono text-ink-500">{c.id}</span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${severityBadgeClass(c.severity)}`}>
                          {severityLabel(c.severity)}
                        </span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${statusBadgeClass(c.status)}`}>
                          {statusLabel(c.status)}
                        </span>
                      </div>
                      <div className="text-sm font-medium text-ink-800 truncate">{c.title}</div>
                      <div className="text-xs text-ink-500 mt-1 flex items-center gap-3">
                        <span>{c.investigator?.name || '未指派'}</span>
                        <span>·</span>
                        <span>{formatDateTime(c.discovered_at)}</span>
                        <span>·</span>
                        <span>{c.area || c.location || '—'}</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-end">
                      <div className="w-16 h-1.5 bg-ink-100 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-brand-500"
                          style={{ width: `${c.progress}%` }}
                        />
                      </div>
                      <span className="text-[10px] text-ink-500 mt-1">{c.progress}%</span>
                    </div>
                  </div>
                 </Link>
              ))
              )}
            </div>
          </CardBody>
        </Card>

        {/* 源系统状态 */}
        <Card>
          <CardHeader>
            <CardTitle>源系统状态</CardTitle>
            <Database className="w-4 h-4 text-ink-400" />
          </CardHeader>
          <CardBody className="p-0">
            <div className="divide-y divide-ink-200">
              {sourcesError ? (
                <ErrorBanner message="源系统状态加载失败" />
              ) : !sources?.length ? (
                <div className="px-5 py-8 text-center text-sm text-ink-500">
                  {sources === undefined ? '加载中...' : '暂无源系统'}
                </div>
              ) : (
                sources.map((s) => (
                <div key={s.code} className="px-5 py-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded ${sourceBadgeClass(s.code)}`}>
                      {s.code}
                    </span>
                    <span className="text-xs text-ink-700">{s.label.split('—')[0]}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`w-2 h-2 rounded-full ${
                        s.health_status === 'healthy' ? 'bg-success-500' :
                        s.health_status === 'degraded' ? 'bg-warning-500' : 'bg-danger-500'
                      }`}
                    />
                    <span className="text-xs text-ink-500">{s.health_status}</span>
                  </div>
                </div>
              ))
              )}
            </div>
          </CardBody>
        </Card>
      </div>

      {/* 最近审计日志 */}
      <Card>
        <CardHeader>
          <CardTitle>最近审计日志</CardTitle>
          <Link to="/audit" className="text-xs text-brand-600 hover:underline">
            查看全部 →
          </Link>
        </CardHeader>
        <CardBody className="p-0">
          <div className="divide-y divide-ink-200">
            {auditError ? (
              <ErrorBanner message="审计日志加载失败" />
            ) : !auditData?.items?.length ? (
              <div className="px-5 py-8 text-center text-sm text-ink-500">
                {auditData === undefined ? '加载中...' : '暂无审计日志'}
              </div>
            ) : (
              auditData.items.map((log) => (
              <div key={log.log_id} className="px-5 py-3 flex items-center gap-3">
                <span className="text-xs font-mono text-ink-400 w-12">#{log.seq}</span>
                <Badge variant="default">{log.action}</Badge>
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-ink-700 truncate">
                    {log.target && <span className="font-mono text-ink-500 mr-2">{log.target}</span>}
                    {log.detail}
                  </div>
                  <div className="text-[10px] text-ink-500 mt-0.5">
                    {log.operator} · {timeAgo(log.created_at)}
                  </div>
                </div>
                <span className="text-[10px] font-mono text-ink-400">
                  {log.current_hash.slice(0, 10)}...
                </span>
              </div>
            ))
            )}
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="px-5 py-8 text-center">
      <div className="text-sm text-danger-600 mb-2">{message}</div>
      {onRetry && (
        <button onClick={onRetry} className="text-xs text-brand-600 hover:underline">
          重试
        </button>
      )}
    </div>
  );
}

function KpiCard({
  title, value, trend, icon: Icon, color, loading, invertTrend,
}: {
  title: string;
  value: string | number;
  trend?: number;
  icon: React.ComponentType<{ className?: string }>;
  color: 'brand' | 'success' | 'warning' | 'danger';
  loading?: boolean;
  invertTrend?: boolean;
}) {
  const colors = {
    brand: 'bg-brand-50 text-brand-600',
    success: 'bg-success-50 text-success-600',
    warning: 'bg-warning-50 text-warning-600',
    danger: 'bg-danger-50 text-danger-600',
  };
  const showTrend = trend !== undefined && trend !== 0;
  const isGood = invertTrend ? (trend ?? 0) < 0 : (trend ?? 0) > 0;

  return (
    <Card className="card-hover">
      <CardBody>
        <div className="flex items-start justify-between mb-3">
          <span className="text-xs font-medium text-ink-500">{title}</span>
          <div className={`w-8 h-8 rounded-md flex items-center justify-center ${colors[color]}`}>
            <Icon className="w-4 h-4" />
          </div>
        </div>
        <div className="text-2xl font-bold text-ink-900">
          {loading ? <span className="shimmer inline-block w-12 h-6 rounded" /> : value}
        </div>
        {showTrend && (
          <div className={`flex items-center gap-1 mt-2 text-xs ${
            isGood ? 'text-success-600' : 'text-danger-600'
          }`}>
            {isGood ? <TrendingDown className="w-3 h-3" /> : <TrendingUp className="w-3 h-3" />}
            <span>{Math.abs(trend!)}</span>
            <span className="text-ink-400">较上周</span>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

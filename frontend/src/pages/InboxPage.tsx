/**
 * 调查收件箱 — 偏差案例列表 + 筛选
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Search, Filter, Plus, Calendar, User, MapPin } from 'lucide-react';
import { caseApi } from '@/services/case.service';
import { Card, CardBody } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import {
  formatDateTime, severityBadgeClass, severityLabel,
  statusBadgeClass, statusLabel,
} from '@/lib/utils';

const STATUS_FILTERS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'investigating', label: '调查中' },
  { value: 'reviewing', label: '审核中' },
  { value: 'closed', label: '已结案' },
];

const SEVERITY_FILTERS = [
  { value: '', label: '全部' },
  { value: 'critical', label: '严重' },
  { value: 'major', label: '重大' },
  { value: 'minor', label: '次要' },
];

export function InboxPage() {
  const [keyword, setKeyword] = useState('');
  const [status, setStatus] = useState('');
  const [severity, setSeverity] = useState('');
  const [page, setPage] = useState(1);

  const { data, isLoading } = useQuery({
    queryKey: ['cases', { keyword, status, severity, page }],
    queryFn: () => caseApi.list({
      keyword: keyword || undefined,
      status: status || undefined,
      severity: severity || undefined,
      page,
      page_size: 20,
    }),
  });

  return (
    <div className="p-6 space-y-4">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-ink-900">调查收件箱</h1>
          <p className="text-sm text-ink-500 mt-1">
            共 {data?.total ?? 0} 个偏差案例
          </p>
        </div>
        <Button>
          <Plus className="w-4 h-4" /> 新建偏差
        </Button>
      </div>

      {/* 筛选栏 */}
      <Card>
        <CardBody className="flex items-center gap-3 flex-wrap">
          <div className="relative flex-1 min-w-64">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-400" />
            <input
              type="text"
              value={keyword}
              onChange={(e) => { setKeyword(e.target.value); setPage(1); }}
              placeholder="搜索案例 ID 或标题..."
              className="w-full h-9 pl-10 pr-3 rounded-md border border-ink-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>

          <div className="flex items-center gap-1.5">
            <Filter className="w-3.5 h-3.5 text-ink-400" />
            <span className="text-xs text-ink-500">状态:</span>
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => { setStatus(f.value); setPage(1); }}
                className={`px-2 py-1 rounded text-xs transition-colors ${
                  status === f.value
                    ? 'bg-brand-500 text-white'
                    : 'bg-ink-100 text-ink-600 hover:bg-ink-200'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-xs text-ink-500">严重:</span>
            {SEVERITY_FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => { setSeverity(f.value); setPage(1); }}
                className={`px-2 py-1 rounded text-xs transition-colors ${
                  severity === f.value
                    ? 'bg-brand-500 text-white'
                    : 'bg-ink-100 text-ink-600 hover:bg-ink-200'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </CardBody>
      </Card>

      {/* 案例列表 */}
      <div className="space-y-2">
        {isLoading && (
          <>
            {[1, 2, 3].map((i) => (
              <Card key={i}>
                <CardBody>
                  <div className="shimmer h-4 w-1/3 rounded mb-2" />
                  <div className="shimmer h-3 w-2/3 rounded" />
                </CardBody>
              </Card>
            ))}
          </>
        )}

        {!isLoading && data?.items?.length === 0 && (
          <Card>
            <CardBody className="text-center py-12">
              <div className="text-ink-400 text-sm">没有匹配的案例</div>
            </CardBody>
          </Card>
        )}

        {!isLoading && data?.items?.map((c) => (
          <Link key={c.id} to={`/investigation/${c.id}`}>
            <Card className="card-hover cursor-pointer">
              <CardBody>
                <div className="flex items-start gap-4">
                  {/* 严重等级指示 */}
                  <div className={`w-1 h-12 rounded-full ${
                    c.severity === 'critical' || c.severity === 'major' ? 'bg-danger-500' :
                    c.severity === 'minor' ? 'bg-warning-500' : 'bg-ink-400'
                  }`} />

                  <div className="flex-1 min-w-0">
                    {/* 标题行 */}
                    <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                      <span className="text-xs font-mono text-ink-500">{c.id}</span>
                      <span className={`text-xs px-1.5 py-0.5 rounded ${severityBadgeClass(c.severity)}`}>
                        {severityLabel(c.severity)}
                      </span>
                      <span className={`text-xs px-1.5 py-0.5 rounded ${statusBadgeClass(c.status)}`}>
                        {statusLabel(c.status)}
                      </span>
                      {c.case_type && (
                        <Badge variant="brand">{c.case_type}</Badge>
                      )}
                      {c.complexity >= 3 && (
                        <Badge variant="warning">高复杂度</Badge>
                      )}
                    </div>

                    {/* 标题 */}
                    <h3 className="text-sm font-semibold text-ink-800 mb-2 line-clamp-1">
                      {c.title}
                    </h3>

                    {/* 元信息 */}
                    <div className="flex items-center gap-4 text-xs text-ink-500 flex-wrap">
                      <span className="flex items-center gap-1">
                        <User className="w-3 h-3" />
                        {c.investigator?.name || '未指派'}
                      </span>
                      <span className="flex items-center gap-1">
                        <Calendar className="w-3 h-3" />
                        {formatDateTime(c.discovered_at)}
                      </span>
                      <span className="flex items-center gap-1">
                        <MapPin className="w-3 h-3" />
                        {c.area || c.location || '—'}
                      </span>
                      {c.organism && (
                        <span className="text-danger-600">● {c.organism}</span>
                      )}
                    </div>
                  </div>

                  {/* 进度 */}
                  <div className="flex flex-col items-end gap-1.5 min-w-32">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-ink-500">进度</span>
                      <span className="text-sm font-semibold text-ink-800">{c.progress}%</span>
                    </div>
                    <div className="w-32 h-1.5 bg-ink-100 rounded-full overflow-hidden">
                      <div
                        className={`h-full ${
                          c.progress >= 80 ? 'bg-success-500' :
                          c.progress >= 40 ? 'bg-brand-500' :
                          'bg-warning-500'
                        }`}
                        style={{ width: `${c.progress}%` }}
                      />
                    </div>
                    {c.deadline && (
                      <span className="text-[10px] text-ink-400">
                        截止 {formatDateTime(c.deadline)}
                      </span>
                    )}
                  </div>
                </div>
              </CardBody>
            </Card>
          </Link>
        ))}
      </div>

      {/* 分页 */}
      {data && data.total > 20 && (
        <div className="flex items-center justify-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
          >
            上一页
          </Button>
          <span className="text-sm text-ink-500">
            第 {page} 页 / 共 {Math.ceil(data.total / 20)} 页
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage((p) => p + 1)}
            disabled={page * 20 >= data.total}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}

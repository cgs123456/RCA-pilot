/**
 * 审计追踪页 — hash 链日志 + 完整性验证 + 导出
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import {
  ShieldCheck, ShieldAlert, Download, Search, RefreshCw,
} from 'lucide-react';
import { auditApi } from '@/services/audit.service';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { formatDateTime, timeAgo, shortHash } from '@/lib/utils';

export function AuditPage() {
  const [params, setParams] = useSearchParams();
  const [keyword, setKeyword] = useState(params.get('case_id') || '');

  const { data: logs, refetch } = useQuery({
    queryKey: ['audit-logs', keyword],
    queryFn: () => auditApi.list({
      case_id: keyword || undefined,
      page: 1,
      page_size: 100,
    }),
  });

  const { data: verify, refetch: refetchVerify } = useQuery({
    queryKey: ['audit-verify'],
    queryFn: auditApi.verify,
  });

  return (
    <div className="p-6 space-y-4">
      {/* 标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-ink-900">审计追踪</h1>
          <p className="text-sm text-ink-500 mt-1">
            GxP 不可篡改审计日志 — hash 链完整性保护
          </p>
        </div>
        <div className="flex gap-2">
          <a href={auditApi.exportUrl('csv', keyword || undefined)} target="_blank" rel="noreferrer">
            <Button variant="outline" size="sm">
              <Download className="w-3.5 h-3.5" /> 导出 CSV
            </Button>
          </a>
          <a href={auditApi.exportUrl('json', keyword || undefined)} target="_blank" rel="noreferrer">
            <Button variant="outline" size="sm">
              <Download className="w-3.5 h-3.5" /> 导出 JSON
            </Button>
          </a>
        </div>
      </div>

      {/* 完整性验证 */}
      <Card className={verify?.valid ? 'border-success-200' : 'border-danger-200'}>
        <CardBody className="flex items-center gap-3">
          {verify?.valid ? (
            <ShieldCheck className="w-6 h-6 text-success-600" />
          ) : (
            <ShieldAlert className="w-6 h-6 text-danger-600" />
          )}
          <div className="flex-1">
            <div className="text-sm font-semibold text-ink-800">
              Hash 链完整性: {verify?.valid ? '通过' : '失败'}
            </div>
            <div className="text-xs text-ink-500">
              共 {verify?.total_logs ?? 0} 条日志 · 验证时间 {verify ? timeAgo(verify.verified_at) : '—'}
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={() => refetchVerify()}>
            <RefreshCw className="w-3.5 h-3.5" /> 重新验证
          </Button>
        </CardBody>
      </Card>

      {/* 搜索栏 */}
      <Card>
        <CardBody className="flex items-center gap-3">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-400" />
            <input
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="按案例 ID 过滤..."
              className="w-full h-9 pl-10 pr-3 rounded-md border border-ink-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            <RefreshCw className="w-3.5 h-3.5" /> 刷新
          </Button>
        </CardBody>
      </Card>

      {/* 日志列表 */}
      <Card>
        <CardHeader>
          <CardTitle>审计日志（共 {logs?.total ?? 0} 条）</CardTitle>
        </CardHeader>
        <CardBody className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-ink-50 text-ink-500">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">序号</th>
                  <th className="px-4 py-2 text-left font-medium">时间</th>
                  <th className="px-4 py-2 text-left font-medium">操作员</th>
                  <th className="px-4 py-2 text-left font-medium">动作</th>
                  <th className="px-4 py-2 text-left font-medium">目标</th>
                  <th className="px-4 py-2 text-left font-medium">详情</th>
                  <th className="px-4 py-2 text-left font-medium">Hash</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-200">
                {logs?.items?.map((log) => (
                  <tr key={log.log_id} className="hover:bg-ink-50">
                    <td className="px-4 py-2 font-mono text-ink-400">#{log.seq}</td>
                    <td className="px-4 py-2 text-ink-600 whitespace-nowrap">
                      {formatDateTime(log.created_at)}
                    </td>
                    <td className="px-4 py-2 text-ink-700">
                      {log.operator}
                      {log.ai_model && (
                        <span className="ml-1 text-[10px] text-brand-600">[AI]</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <Badge variant={
                        log.action.includes('FAIL') || log.action.includes('REJECT') ? 'danger' :
                        log.action.includes('SUCCESS') || log.action.includes('ACCEPT') ? 'success' :
                        log.action.includes('AGENT') ? 'brand' : 'default'
                      }>
                        {log.action}
                      </Badge>
                    </td>
                    <td className="px-4 py-2 font-mono text-ink-500">{log.target || '—'}</td>
                    <td className="px-4 py-2 text-ink-600 max-w-xs truncate" title={log.detail || ''}>
                      {log.detail || '—'}
                    </td>
                    <td className="px-4 py-2 font-mono text-ink-400 text-[10px]">
                      {shortHash(log.current_hash, 10)}
                    </td>
                  </tr>
                ))}
                {(!logs?.items || logs.items.length === 0) && (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-ink-400">
                      暂无审计日志
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

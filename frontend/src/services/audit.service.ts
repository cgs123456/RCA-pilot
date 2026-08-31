/**
 * Audit Service API
 */
import api from './api-client';
import type { AuditLog, Page } from '@/types';

export interface ExportAuditParams {
  format: 'csv' | 'json';
  case_id?: string;
  start?: string;
  end?: string;
}

export const auditApi = {
  list: (params: {
    case_id?: string;
    operator?: string;
    action?: string;
    start?: string;
    end?: string;
    page?: number;
    page_size?: number;
  } = {}) => api.get<Page<AuditLog>>('/audit/logs', { params }).then((r) => r.data),

  verify: () => api.get('/audit/verify').then((r) => r.data),

  /**
   * 导出审计包（blob 下载）
   * 旧实现用 <a href target="_blank"> 跳转：路径拼错（缺 /v1）且不带 JWT，100% 失败。
   * 现走 axios 携带 Authorization，responseType blob，由调用方触发浏览器下载。
   */
  exportAudit: async ({ format, case_id, start, end }: ExportAuditParams): Promise<void> => {
    const resp = await api.get('/audit/export', {
      params: { format, case_id, start, end },
      responseType: 'blob',
    });
    const blob = new Blob([resp.data]);
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement('a');
      const ts = new Date().toISOString().replace(/[:.]/g, '').slice(0, 15) + 'Z';
      a.href = url;
      a.download = `audit_export_${ts}.${format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      URL.revokeObjectURL(url);
    }
  },
};

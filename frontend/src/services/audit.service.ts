/**
 * Audit Service API
 */
import api from './api-client';
import type { AuditLog, Page } from '@/types';

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

  exportUrl: (format: 'csv' | 'json' = 'csv', caseId?: string) => {
    const params = new URLSearchParams();
    params.set('format', format);
    if (caseId) params.set('case_id', caseId);
    return `/api/audit/export?${params.toString()}`;
  },
};

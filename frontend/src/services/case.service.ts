/**
 * Case Service API
 */
import api from './api-client';
import type {
  Deviation,
  DeviationDetail,
  KPI,
  Page,
  HitlGateResponse,
  HitlAction,
  SourceSystemInfo,
} from '@/types';

export interface ListCasesParams {
  status?: string;
  severity?: string;
  investigator_id?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export const caseApi = {
  list: (params: ListCasesParams = {}) =>
    api.get<Page<Deviation>>('/cases', { params }).then((r) => r.data),

  get: (id: string) =>
    api.get<DeviationDetail>(`/cases/${id}`).then((r) => r.data),

  create: (body: any) =>
    api.post<Deviation>('/cases', body).then((r) => r.data),

  update: (id: string, body: any) =>
    api.patch<Deviation>(`/cases/${id}`, body).then((r) => r.data),

  hitl: (caseId: string, gateId: string, action: HitlAction, comment?: string) =>
    api.put<HitlGateResponse>(`/cases/${caseId}/hitl/${gateId}`, {
      gate_id: gateId,
      action,
      comment,
    }).then((r) => r.data),

  getKpi: () =>
    api.get<KPI>('/dashboard/kpi').then((r) => r.data),

  getSourceSystems: () =>
    api.get<SourceSystemInfo[]>('/source-systems').then((r) => r.data),
};

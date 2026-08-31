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

/** 创建偏差请求体（与后端 DeviationCreate 对齐） */
export interface CreateCaseBody {
  id?: string;
  title: string;
  description?: string;
  severity?: 'critical' | 'major' | 'minor' | 'info';
  case_type?: string;
  complexity?: number;
  location?: string;
  area?: string;
  shift?: string;
  investigator_id?: string;
  qa_reviewer_id?: string;
  discovered_at: string;
  deadline?: string;
  organism?: string;
  organism_source?: string;
  product_impact?: string;
  immediate_action?: string;
}

/** 更新偏差请求体（与后端 DeviationUpdate 对齐；progress 由系统管控不可提交） */
export interface UpdateCaseBody {
  title?: string;
  description?: string;
  severity?: 'critical' | 'major' | 'minor';
  status?: 'investigating';
  investigator_id?: string;
  qa_reviewer_id?: string;
  deadline?: string;
  immediate_action?: string;
  meta?: Record<string, unknown>;
}

/** gate2（5 Whys）人工编辑载荷 */
export interface HitlEditPayload {
  five_whys?: { n: number; why_text: string }[];
}

export const caseApi = {
  list: (params: ListCasesParams = {}) =>
    api.get<Page<Deviation>>('/cases', { params }).then((r) => r.data),

  get: (id: string) =>
    api.get<DeviationDetail>(`/cases/${id}`).then((r) => r.data),

  create: (body: CreateCaseBody) =>
    api.post<Deviation>('/cases', body).then((r) => r.data),

  update: (id: string, body: UpdateCaseBody) =>
    api.patch<Deviation>(`/cases/${id}`, body).then((r) => r.data),

  hitl: (caseId: string, gateId: string, action: HitlAction, comment?: string, editPayload?: HitlEditPayload) =>
    api.put<HitlGateResponse>(`/cases/${caseId}/hitl/${gateId}`, {
      gate_id: gateId,
      action,
      comment,
      edit_payload: editPayload,
    }).then((r) => r.data),

  getKpi: () =>
    api.get<KPI>('/dashboard/kpi').then((r) => r.data),

  getSourceSystems: () =>
    api.get<SourceSystemInfo[]>('/source-systems').then((r) => r.data),
};

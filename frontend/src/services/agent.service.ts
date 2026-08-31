/**
 * Agent Service API
 */
import api from './api-client';
import type { AgentRun, Page } from '@/types';

export const agentApi = {
  run: (caseId: string, triggeredBy?: string) =>
    api.post<AgentRun>('/agent/pipeline/run', {
      case_id: caseId,
      triggered_by: triggeredBy,
    }).then((r) => r.data),

  get: (runId: string) =>
    api.get<AgentRun>(`/agent/runs/${runId}`).then((r) => r.data),

  list: (params: { case_id?: string; status?: string; page?: number; page_size?: number } = {}) =>
    api.get<Page<AgentRun>>('/agent/runs', { params }).then((r) => r.data),

  pause: (runId: string) =>
    api.post(`/agent/runs/${runId}/pause`).then((r) => r.data),

  cancel: (runId: string) =>
    api.post(`/agent/runs/${runId}/cancel`).then((r) => r.data),

  resume: (runId: string) =>
    api.post(`/agent/runs/${runId}/resume`).then((r) => r.data),
};

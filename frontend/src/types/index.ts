/**
 * RCA Pilot — 类型定义（与后端 schemas 对齐）
 */

export type Severity = 'critical' | 'major' | 'minor' | 'info';
export type CaseStatus = 'pending' | 'investigating' | 'reviewing' | 'closed';
export type HitlGateStatus = 'pending' | 'done' | 'rejected';
export type HitlAction = 'accept' | 'reject' | 'edit';
export type SourceSystem = 'LIMS' | 'MES' | 'EMS' | 'CMMS' | 'LMS' | 'BMS';
/** P2-⑧ 偏差类型（与后端 DeviationType 对齐：OOS=检验结果超标 / OOT=趋势外） */
export type DeviationType =
  | 'process_deviation'
  | 'lab_oos'
  | 'lab_oot'
  | 'complaint'
  | 'equipment'
  | 'environment'
  | 'other';

/** ICH Q9 FMEA 三分量（与后端 FmeaScores 对齐：RPN = S×O×D，各 1-10） */
export interface FmeaScores {
  severity_score: number;
  occurrence: number;
  detection: number;
}

export interface User {
  id: string;
  email: string;
  name: string;
  role: 'admin' | 'investigator' | 'qa_reviewer' | 'ops_manager' | 'auditor' | 'operator';
  dept?: string;
  title?: string;
  avatar?: string;
  color?: string;
  shift?: string;
  cert?: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Deviation {
  id: string;
  title: string;
  description?: string;
  severity: Severity;
  status: CaseStatus;
  deviation_type: DeviationType;
  affected_batches: string[];
  case_type?: string;
  complexity: number;
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
  progress: number;
  closed_at?: string;
  /** ICH Q9 风险评估（PATCH /cases/{id}/risk 维护；创建含 fmea 时初始化） */
  risk_rpn?: number;
  risk_category?: string;
  risk_owner?: string;
  hitl_gates: Record<string, HitlGateStatus>;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  investigator?: User;
  qa_reviewer?: User;
}

export interface Evidence {
  id: string;
  case_id?: string;
  source_system: SourceSystem;
  collected_at: string;
  summary?: string;
  raw_data: Record<string, unknown>;
  content_hash: string;
  confidence: number;
  is_signed: boolean;
  created_at: string;
}

export interface TimelineEvent {
  id: string;
  case_id: string;
  occurred_at: string;
  source: string;
  title: string;
  detail?: string;
  severity: string;
  evidence_id?: string;
  is_anomaly: boolean;
  sort_order: number;
}

export interface FiveWhy {
  id: string;
  case_id: string;
  n: number;
  question?: string;
  answer?: string;
  status: string;
  evidence_ids: string[];
  is_root_cause: boolean;
  ai_model?: string;
  ai_output_hash?: string;
}

export interface SkepticChallenge {
  id: string;
  case_id: string;
  target: string;
  challenge: string;
  evidence_ids: string[];
  status: string;
  resolution?: string;
  created_at: string;
  resolved_at?: string;
}

export interface DeviationDetail extends Deviation {
  evidence: Evidence[];
  timeline: TimelineEvent[];
  five_whys: FiveWhy[];
  skeptic_challenges: SkepticChallenge[];
}

export interface KPI {
  unsolved_count: number;
  major_count_this_month: number;
  avg_cycle_days: number;
  capa_overdue_rate: number;
  trend_unsolved_delta: number;
  trend_major_delta: number;
  trend_cycle_delta: number;
  trend_capa_delta: number;
}

export interface AuditLog {
  log_id: string;
  seq: number;
  operator: string;
  action: string;
  target?: string;
  detail?: string;
  ip_address?: string;
  session_id?: string;
  evidence_refs: string[];
  ai_model?: string;
  ai_prompt_hash?: string;
  ai_output_hash?: string;
  trace_id?: string;
  prev_hash: string;
  current_hash: string;
  created_at: string;
}

export interface AgentRun {
  id: string;
  case_id: string;
  status: string;
  current_agent?: string;
  error_message?: string;
  started_at?: string;
  completed_at?: string;
  agent_states: Record<string, unknown>;
  triggered_by: string;
  created_at: string;
}

export interface HitlGateResponse {
  case_id: string;
  gate_id: string;
  action: HitlAction;
  new_status: string;
  all_gates_passed: boolean;
  triggered_agent?: string;
}

export interface SourceSystemInfo {
  code: string;
  label: string;
  icon: string;
  color: string;
  description?: string;
  health_status: string;
  last_sync_at?: string;
  is_readonly: boolean;
}

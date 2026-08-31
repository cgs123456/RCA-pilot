/**
 * 调查工作台 — 案例详情 + HITL 关卡审批 + Agent 运行控制
 *
 * 核心修复：HITL 审批 UI（旧版 hitlMutation 是从未调用的死代码，5 道关卡无法通过）；
 * Agent 流水线状态轮询（旧版启动后界面永久停留在快照）；暂停/取消/恢复控制。
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useParams, Link } from 'react-router-dom';
import {
  ArrowLeft, Play, CheckCircle, XCircle, Edit3,
  Clock, User, MapPin, Calendar, AlertTriangle,
  Activity, FileText, Shield, FlaskConical, Brain,
  Zap, Pause, Ban, RotateCcw, Pencil,
} from 'lucide-react';
import { caseApi } from '@/services/case.service';
import { agentApi } from '@/services/agent.service';
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { useToast } from '@/components/ui/Toast';
import {
  formatDateTime, severityBadgeClass, severityLabel,
  sourceBadgeClass, statusBadgeClass, statusLabel, gateLabel, shortHash,
  roleLabel,
} from '@/lib/utils';
import type {
  DeviationDetail, HitlAction, TimelineEvent, Evidence, FiveWhy,
  SkepticChallenge, AgentRun,
} from '@/types';

const GATES = ['gate1', 'gate2', 'gate3', 'gate4', 'gate5'];

const TABS = [
  { id: 'timeline', label: '时间线', icon: Clock },
  { id: 'evidence', label: '证据', icon: FlaskConical },
  { id: 'five-whys', label: '5 Whys', icon: Brain },
  { id: 'skeptic', label: 'Skeptic 反例', icon: AlertTriangle },
  { id: 'agent', label: 'Agent 运行', icon: Activity },
  { id: 'report', label: '报告草稿', icon: FileText },
];

export function InvestigationPage() {
  const { caseId = '' } = useParams();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [tab, setTab] = useState('timeline');
  const [runAgentModal, setRunAgentModal] = useState(false);

  const { data: detail, isLoading, isError, refetch } = useQuery<DeviationDetail>({
    queryKey: ['case', caseId],
    queryFn: () => caseApi.get(caseId),
    enabled: !!caseId,
  });

  // 流水线进行中每 3s 轮询进度（旧版从不轮询，界面永久停留在快照）
  const { data: agentRuns } = useQuery({
    queryKey: ['agent-runs', caseId],
    queryFn: () => agentApi.list({ case_id: caseId, page: 1, page_size: 5 }),
    enabled: !!caseId,
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? [];
      const hasActive = items.some((r) => r.status === 'running' || r.status === 'pending');
      return hasActive ? 3000 : false;
    },
  });

  const invalidateCaseData = () => {
    queryClient.invalidateQueries({ queryKey: ['case', caseId] });
    queryClient.invalidateQueries({ queryKey: ['agent-runs', caseId] });
    queryClient.invalidateQueries({ queryKey: ['cases'] });
    queryClient.invalidateQueries({ queryKey: ['kpi'] });
  };

  // HITL 审批 mutation —— 旧版定义后从未调用（死代码），本版接入完整审批 UI
  const hitlMutation = useMutation({
    mutationFn: ({
      gateId, action, comment, editPayload,
    }: {
      gateId: string;
      action: HitlAction;
      comment?: string;
      editPayload?: { five_whys: { n: number; why_text: string }[] };
    }) => caseApi.hitl(caseId, gateId, action, comment, editPayload),
    onSuccess: (data) => {
      invalidateCaseData();
      if (data.all_gates_passed) {
        toast('全部关卡已通过，案例进入审核（reviewing）状态', 'success');
      } else {
        toast(`${gateLabel(data.gate_id)} 处理完成`, 'success');
      }
    },
    onError: (e) => toast((e as { message?: string })?.message || 'HITL 操作失败', 'error'),
  });

  const runAgentMutation = useMutation({
    mutationFn: () => agentApi.run(caseId),
    onSuccess: () => {
      invalidateCaseData();
      setRunAgentModal(false);
      toast('Agent 流水线已启动', 'success');
    },
    onError: (e) => toast((e as { message?: string })?.message || '启动失败（可能已有进行中的流水线）', 'error'),
  });

  const runControlMutation = useMutation({
    mutationFn: ({ runId, op }: { runId: string; op: 'pause' | 'cancel' | 'resume' }) => {
      if (op === 'pause') return agentApi.pause(runId);
      if (op === 'cancel') return agentApi.cancel(runId);
      return agentApi.resume(runId);
    },
    onSuccess: (_d, vars) => {
      invalidateCaseData();
      toast(
        vars.op === 'pause' ? '流水线已暂停' : vars.op === 'cancel' ? '流水线已取消' : '流水线已恢复',
        'success',
      );
    },
    onError: (e) => toast((e as { message?: string })?.message || '操作失败', 'error'),
  });

  if (isLoading) {
    return (
      <div className="p-6 space-y-4">
        <div className="shimmer h-8 w-1/2 rounded" />
        <div className="shimmer h-32 rounded" />
        <div className="shimmer h-64 rounded" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="p-6">
        <Card>
          <CardBody className="text-center py-12">
            <div className="text-danger-600 text-sm mb-2">案例加载失败</div>
            <button onClick={() => refetch()} className="text-xs text-brand-600 hover:underline">重试</button>
          </CardBody>
        </Card>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="p-6">
        <Card>
          <CardBody className="text-center py-12">
            <div className="text-ink-400">案例 {caseId} 不存在</div>
            <Link to="/inbox" className="text-brand-600 text-sm mt-2 inline-block">
              ← 返回收件箱
            </Link>
          </CardBody>
        </Card>
      </div>
    );
  }

  const gates = detail.hitl_gates || {};
  const latestRun = agentRuns?.items?.[0];
  const pipelineActive = latestRun?.status === 'running' || latestRun?.status === 'pending';

  return (
    <div className="flex flex-col h-full">
      {/* 案例头部 */}
      <div className="bg-white border-b border-ink-200 px-6 py-4">
        <div className="flex items-center gap-3 mb-3">
          <Link to="/inbox" className="p-1.5 rounded hover:bg-ink-100">
            <ArrowLeft className="w-4 h-4 text-ink-500" />
          </Link>
          <span className="text-xs font-mono text-ink-500">{detail.id}</span>
          <span className={`text-xs px-1.5 py-0.5 rounded ${severityBadgeClass(detail.severity)}`}>
            {severityLabel(detail.severity)}
          </span>
          <span className={`text-xs px-1.5 py-0.5 rounded ${statusBadgeClass(detail.status)}`}>
            {statusLabel(detail.status)}
          </span>
          {detail.case_type && <Badge variant="brand">{detail.case_type}</Badge>}
          <div className="flex-1" />
          <Button
            variant="primary"
            size="sm"
            onClick={() => setRunAgentModal(true)}
            disabled={pipelineActive}
            title={pipelineActive ? '已有进行中的流水线' : undefined}
          >
            <Play className="w-3.5 h-3.5" /> 启动 Agent
          </Button>
        </div>

        <h1 className="text-xl font-bold text-ink-900 mb-3">{detail.title}</h1>

        {/* 元信息 */}
        <div className="flex items-center gap-4 text-xs text-ink-500 flex-wrap">
          <span className="flex items-center gap-1">
            <User className="w-3 h-3" />
            调查员: {detail.investigator?.name || '—'}
            {detail.investigator && <span className="text-ink-400">({roleLabel(detail.investigator.role)})</span>}
          </span>
          <span className="flex items-center gap-1">
            <Shield className="w-3 h-3" />
            QA: {detail.qa_reviewer?.name || '—'}
          </span>
          <span className="flex items-center gap-1">
            <Calendar className="w-3 h-3" />
            发现: {formatDateTime(detail.discovered_at)}
          </span>
          {detail.deadline && (
            <span className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              截止: {formatDateTime(detail.deadline)}
            </span>
          )}
          <span className="flex items-center gap-1">
            <MapPin className="w-3 h-3" />
            {detail.area || detail.location || '—'}
          </span>
          {detail.organism && (
            <span className="text-danger-600">● {detail.organism}</span>
          )}
        </div>

        {/* HITL 关卡进度条（每关带状态徽章，审批按钮在下方 HITL 面板） */}
        <div className="mt-4 flex items-center gap-2">
          {GATES.map((g, i) => {
            const status = gates[g] || 'pending';
            return (
              <div key={g} className="flex items-center">
                <div
                  className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs ${
                    status === 'done' ? 'bg-success-50 text-success-600' :
                    status === 'rejected' ? 'bg-danger-50 text-danger-600' :
                    'bg-ink-100 text-ink-500'
                  }`}
                  title={gateLabel(g)}
                >
                  {status === 'done' ? <CheckCircle className="w-3 h-3" /> :
                   status === 'rejected' ? <XCircle className="w-3 h-3" /> :
                   <Clock className="w-3 h-3" />}
                  <span>{gateLabel(g)}</span>
                </div>
                {i < GATES.length - 1 && (
                  <div className={`w-4 h-px ${status === 'done' ? 'bg-success-500' : 'bg-ink-200'}`} />
                )}
              </div>
            );
          })}
          <div className="ml-auto text-xs text-ink-500">
            进度 <span className="font-semibold text-ink-800">{detail.progress}%</span>
          </div>
        </div>
      </div>

      {/* HITL 审批面板 —— GxP 核心交互：5 道关卡按顺序审批 */}
      {detail.status !== 'closed' && (
        <HitlPanel
          gates={gates}
          detail={detail}
          pending={hitlMutation.isPending}
          onSubmit={(gateId, action, comment, editPayload) =>
            hitlMutation.mutate({ gateId, action, comment, editPayload })}
        />
      )}

      {/* 描述 + 立即措施 */}
      {(detail.description || detail.immediate_action) && (
        <div className="bg-white border-b border-ink-200 px-6 py-3 grid grid-cols-1 md:grid-cols-2 gap-4">
          {detail.description && (
            <div>
              <div className="text-xs font-semibold text-ink-700 mb-1">偏差描述</div>
              <p className="text-xs text-ink-600 leading-relaxed">{detail.description}</p>
            </div>
          )}
          {detail.immediate_action && (
            <div>
              <div className="text-xs font-semibold text-warning-600 mb-1 flex items-center gap-1">
                <Zap className="w-3 h-3" /> 立即措施
              </div>
              <pre className="text-xs text-ink-600 whitespace-pre-wrap font-sans leading-relaxed">
                {detail.immediate_action}
              </pre>
            </div>
          )}
        </div>
      )}

      {/* 标签栏 */}
      <div className="bg-white border-b border-ink-200 px-6 flex items-center gap-1 overflow-x-auto">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = tab === t.id;
          const count =
            t.id === 'evidence' ? detail.evidence.length :
            t.id === 'five-whys' ? detail.five_whys.length :
            t.id === 'skeptic' ? detail.skeptic_challenges.length :
            t.id === 'agent' ? agentRuns?.total ?? 0 : undefined;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium border-b-2 transition-colors whitespace-nowrap ${
                active
                  ? 'border-brand-500 text-brand-600'
                  : 'border-transparent text-ink-500 hover:text-ink-700'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              {t.label}
              {count !== undefined && count > 0 && (
                <span className="px-1.5 py-0 rounded-full bg-ink-100 text-ink-600 text-[10px]">
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-auto p-6 bg-ink-50">
        {tab === 'timeline' && <TimelineTab events={detail.timeline} />}
        {tab === 'evidence' && <EvidenceTab evidence={detail.evidence} />}
        {tab === 'five-whys' && <FiveWhysTab whys={detail.five_whys} />}
        {tab === 'skeptic' && <SkepticTab challenges={detail.skeptic_challenges} />}
        {tab === 'agent' && (
          <AgentTab
            runs={agentRuns?.items || []}
            controlPending={runControlMutation.isPending}
            onRunAgent={() => setRunAgentModal(true)}
            onControl={(runId, op) => runControlMutation.mutate({ runId, op })}
          />
        )}
        {tab === 'report' && <ReportTab detail={detail} />}
      </div>

      {/* 启动 Agent Modal */}
      {runAgentModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 animate-fade-in" onClick={() => setRunAgentModal(false)}>
          <Card className="w-full max-w-md animate-scale-in" >
            <CardBody>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-10 h-10 rounded-md bg-brand-50 flex items-center justify-center">
                  <Activity className="w-5 h-5 text-brand-600" />
                </div>
                <div>
                  <h3 className="text-sm font-semibold text-ink-800">启动 Agent 流水线</h3>
                  <p className="text-xs text-ink-500">将依次执行 6 个 Agent，每步写审计日志</p>
                </div>
              </div>
              <div className="space-y-1.5 mb-4">
                {['Meta-Controller', 'Evidence Hunter', 'Reasoner', 'Skeptic', 'Drafter', 'Guardian'].map((a, i) => (
                  <div key={a} className="flex items-center gap-2 text-xs text-ink-600">
                    <span className="w-5 h-5 rounded-full bg-ink-100 flex items-center justify-center text-[10px]">{i + 1}</span>
                    {a}
                  </div>
                ))}
              </div>
              <div className="flex gap-2 justify-end">
                <Button variant="outline" size="sm" onClick={() => setRunAgentModal(false)}>取消</Button>
                <Button
                  size="sm"
                  onClick={() => runAgentMutation.mutate()}
                  disabled={runAgentMutation.isPending}
                >
                  {runAgentMutation.isPending ? '启动中...' : '启动'}
                </Button>
              </div>
            </CardBody>
          </Card>
        </div>
      )}
    </div>
  );
}

// ============================================================
// HITL 审批面板
// ============================================================
function HitlPanel({
  gates, detail, pending, onSubmit,
}: {
  gates: Record<string, string>;
  detail: DeviationDetail;
  pending: boolean;
  onSubmit: (
    gateId: string,
    action: HitlAction,
    comment?: string,
    editPayload?: { five_whys: { n: number; why_text: string }[] },
  ) => void;
}) {
  // 按顺序找到第一个可处理的关卡：未 done 的第一关
  const currentGateIdx = GATES.findIndex((g) => gates[g] !== 'done');
  const currentGate = currentGateIdx === -1 ? null : GATES[currentGateIdx];

  const [comment, setComment] = useState('');
  const [editing, setEditing] = useState(false);
  const [editTexts, setEditTexts] = useState<Record<number, string>>({});

  if (!currentGate || detail.status === 'closed') return null;
  const isGate2 = currentGate === 'gate2';

  const submit = (action: HitlAction) => {
    const editPayload =
      action === 'edit' && isGate2
        ? {
            five_whys: Object.entries(editTexts).map(([n, why_text]) => ({
              n: Number(n),
              why_text,
            })),
          }
        : undefined;
    onSubmit(currentGate, action, comment || undefined, editPayload);
    setComment('');
    setEditing(false);
    setEditTexts({});
  };

  return (
    <div className="bg-brand-50/60 border-b border-brand-100 px-6 py-3">
      <div className="flex items-center gap-2 mb-2">
        <Shield className="w-4 h-4 text-brand-600" />
        <span className="text-xs font-semibold text-brand-700">
          HITL 人工决策 — {gateLabel(currentGate)}（第 {currentGateIdx + 1}/5 关）
        </span>
        {currentGateIdx > 0 && (
          <span className="text-[10px] text-ink-400">前置关卡已全部通过</span>
        )}
      </div>

      {isGate2 && editing ? (
        /* gate2 编辑模式：逐条修改 5 Whys 答案 */
        <div className="space-y-2">
          {detail.five_whys.map((w) => (
            <div key={w.id} className="flex items-start gap-2">
              <span className="w-5 h-5 rounded-full bg-brand-100 text-brand-700 flex items-center justify-center text-[10px] font-semibold mt-1">
                {w.n}
              </span>
              <textarea
                value={editTexts[w.n] ?? w.answer ?? ''}
                onChange={(e) => setEditTexts((prev) => ({ ...prev, [w.n]: e.target.value }))}
                rows={2}
                className="flex-1 text-xs rounded-md border border-ink-200 bg-white px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-brand-500"
                placeholder={`Why #${w.n} 的答案`}
              />
            </div>
          ))}
          <div className="flex items-center gap-2 justify-end">
            <input
              type="text"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="编辑说明（写入审计日志）"
              className="h-8 text-xs rounded-md border border-ink-200 bg-white px-2 w-64"
            />
            <Button variant="outline" size="sm" onClick={() => setEditing(false)} disabled={pending}>
              取消
            </Button>
            <Button size="sm" onClick={() => submit('edit')} disabled={pending}>
              <Pencil className="w-3 h-3" /> 保存编辑
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="text"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="审批意见（可选，写入审计日志）"
            className="h-8 flex-1 min-w-48 text-xs rounded-md border border-ink-200 bg-white px-2"
          />
          <Button
            variant="outline"
            size="sm"
            onClick={() => submit('reject')}
            disabled={pending}
            className="text-danger-600 border-danger-200 hover:bg-danger-50"
          >
            <XCircle className="w-3.5 h-3.5" /> 拒绝
          </Button>
          {isGate2 && detail.five_whys.length > 0 && (
            <Button variant="outline" size="sm" onClick={() => setEditing(true)} disabled={pending}>
              <Edit3 className="w-3.5 h-3.5" /> 修改 5 Whys
            </Button>
          )}
          <Button size="sm" onClick={() => submit('accept')} disabled={pending}>
            <CheckCircle className="w-3.5 h-3.5" /> 通过
          </Button>
        </div>
      )}
    </div>
  );
}

// ---- Timeline ----
function TimelineTab({ events }: { events: TimelineEvent[] }) {
  if (!events?.length) return <EmptyTab text="暂无时间线事件" />;
  return (
    <div className="max-w-3xl mx-auto">
      <div className="relative timeline-line pl-8 space-y-3">
        {events.map((e) => (
          <div key={e.id} className="relative">
            <div className={`absolute -left-8 top-1 timeline-dot ${e.is_anomaly ? 'anomaly' : ''}`} />
            <Card className={e.is_anomaly ? 'border-danger-200' : ''}>
              <CardBody className="py-2.5 px-4">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-mono text-ink-500">{formatDateTime(e.occurred_at)}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${sourceBadgeClass(e.source || '')}`}>
                    {e.source || '—'}
                  </span>
                  {e.is_anomaly && <Badge variant="danger">异常</Badge>}
                </div>
                <div className="text-sm font-medium text-ink-800">{e.title}</div>
                {e.detail && <div className="text-xs text-ink-600 mt-1">{e.detail}</div>}
                {e.evidence_id && (
                  <div className="mt-1.5 text-[10px] text-ink-400 font-mono">
                    证据: {e.evidence_id}
                  </div>
                )}
              </CardBody>
            </Card>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- Evidence ----
function EvidenceTab({ evidence }: { evidence: Evidence[] }) {
  if (!evidence?.length) return <EmptyTab text="暂无证据" />;
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      {evidence.map((ev) => (
        <Card key={ev.id} className="card-hover">
          <CardBody>
            <div className="flex items-center gap-2 mb-2">
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${sourceBadgeClass(ev.source_system)}`}>
                {ev.source_system}
              </span>
              <span className="text-[10px] text-ink-400 font-mono">{ev.id}</span>
              <div className="flex-1" />
              <span className="text-[10px] text-ink-500">
                置信度 {ev.confidence}/4
              </span>
            </div>
            <div className="text-xs text-ink-800 leading-relaxed mb-2">{ev.summary}</div>
            <div className="text-[10px] text-ink-400 font-mono">
              {shortHash(ev.content_hash)}
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

// ---- 5 Whys ----
function FiveWhysTab({ whys }: { whys: FiveWhy[] }) {
  if (!whys?.length) return <EmptyTab text="暂无 5 Whys 推理" />;
  return (
    <div className="max-w-3xl mx-auto space-y-3">
      {whys.map((w) => (
        <Card key={w.id} className={w.is_root_cause ? 'border-danger-200 bg-danger-50/30' : ''}>
          <CardBody>
            <div className="flex items-center gap-2 mb-2">
              <span className="w-6 h-6 rounded-full bg-brand-50 text-brand-600 flex items-center justify-center text-xs font-semibold">
                {w.n}
              </span>
              <span className="text-xs font-medium text-ink-700">Why #{w.n}</span>
              {w.is_root_cause && <Badge variant="danger">根因</Badge>}
              <div className="flex-1" />
              {w.ai_model && (
                <span className="text-[10px] text-ink-400 font-mono">{w.ai_model}</span>
              )}
            </div>
            <div className="text-xs text-ink-600 mb-1.5">{w.question}</div>
            <div className="text-sm text-ink-800 pl-3 border-l-2 border-brand-200">
              {w.answer}
            </div>
            {w.evidence_ids?.length > 0 && (
              <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                <span className="text-[10px] text-ink-500">证据:</span>
                {w.evidence_ids.map((eid: string) => (
                  <span key={eid} className="text-[10px] font-mono px-1.5 py-0.5 bg-ink-100 text-ink-600 rounded">
                    {eid}
                  </span>
                ))}
              </div>
            )}
            {w.ai_output_hash && (
              <div className="mt-1.5 text-[10px] text-ink-400 font-mono">
                输出 hash: {shortHash(w.ai_output_hash)}
              </div>
            )}
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

// ---- Skeptic ----
function SkepticTab({ challenges }: { challenges: SkepticChallenge[] }) {
  if (!challenges?.length) return <EmptyTab text="暂无 Skeptic 反例挑战" />;
  return (
    <div className="max-w-3xl mx-auto space-y-3">
      {challenges.map((c) => (
        <Card key={c.id} className="border-warning-200">
          <CardBody>
            <div className="flex items-center gap-2 mb-2">
              <AlertTriangle className="w-4 h-4 text-warning-600" />
              <span className="text-xs font-semibold text-warning-600">Skeptic 挑战</span>
              <Badge variant={c.status === 'accepted' ? 'success' : c.status === 'rejected' ? 'danger' : 'warning'}>
                {c.status === 'accepted' ? '已采纳' : c.status === 'rejected' ? '已驳回' : '待处理'}
              </Badge>
            </div>
            <div className="text-xs text-ink-600 mb-2">
              <span className="font-medium">被挑战的结论:</span> {c.target}
            </div>
            <div className="text-sm text-ink-800 p-2 bg-warning-50/50 rounded">
              {c.challenge}
            </div>
            {c.resolution && (
              <div className="mt-2 text-xs text-ink-600">
                <span className="font-medium">解决方案:</span> {c.resolution}
              </div>
            )}
            {c.evidence_ids?.length > 0 && (
              <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                <span className="text-[10px] text-ink-500">相关证据:</span>
                {c.evidence_ids.map((eid: string) => (
                  <span key={eid} className="text-[10px] font-mono px-1.5 py-0.5 bg-ink-100 text-ink-600 rounded">
                    {eid}
                  </span>
                ))}
              </div>
            )}
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

// ---- Agent 运行（含暂停/取消/恢复控制）----
function AgentTab({
  runs, controlPending, onRunAgent, onControl,
}: {
  runs: AgentRun[];
  controlPending: boolean;
  onRunAgent: () => void;
  onControl: (runId: string, op: 'pause' | 'cancel' | 'resume') => void;
}) {
  if (!runs.length) {
    return (
      <EmptyTab
        text="尚未启动 Agent 流水线"
        action={<Button size="sm" onClick={onRunAgent}><Play className="w-3.5 h-3.5" /> 启动</Button>}
      />
    );
  }
  return (
    <div className="max-w-4xl mx-auto space-y-3">
      {runs.map((r) => (
        <Card key={r.id}>
          <CardHeader>
            <div className="flex items-center gap-2 flex-wrap">
              <Activity className="w-4 h-4 text-brand-500" />
              <span className="text-xs font-mono text-ink-500">{r.id}</span>
              <Badge variant={
                r.status === 'completed' ? 'success' :
                r.status === 'running' ? 'brand' :
                r.status === 'failed' || r.status === 'cancelled' ? 'danger' : 'default'
              }>
                {r.status === 'cancelled' ? '已取消' : r.status}
              </Badge>
              {r.current_agent && (
                <span className="text-xs text-ink-500">当前: {r.current_agent}</span>
              )}
              {/* 流水线控制按钮 */}
              <div className="flex items-center gap-1 ml-2">
                {(r.status === 'running' || r.status === 'pending') && (
                  <>
                    <Button
                      variant="ghost" size="sm"
                      disabled={controlPending}
                      onClick={() => onControl(r.id, 'pause')}
                      title="暂停（当前阶段完成后停止）"
                    >
                      <Pause className="w-3.5 h-3.5" /> 暂停
                    </Button>
                    <Button
                      variant="ghost" size="sm"
                      disabled={controlPending}
                      onClick={() => onControl(r.id, 'cancel')}
                      title="取消（终态）"
                      className="text-danger-600"
                    >
                      <Ban className="w-3.5 h-3.5" /> 取消
                    </Button>
                  </>
                )}
                {r.status === 'paused' && (
                  <Button
                    variant="ghost" size="sm"
                    disabled={controlPending}
                    onClick={() => onControl(r.id, 'resume')}
                    title="恢复（重新执行各阶段）"
                  >
                    <RotateCcw className="w-3.5 h-3.5" /> 恢复
                  </Button>
                )}
              </div>
            </div>
            <span className="text-xs text-ink-400">{formatDateTime(r.created_at)}</span>
          </CardHeader>
          <CardBody>
            {r.error_message && (
              <div className="mb-2 text-xs text-danger-600 p-2 bg-danger-50 rounded">
                {r.error_message}
              </div>
            )}
            {/* Agent 状态时间线 */}
            <div className="space-y-2">
              {Object.entries(r.agent_states || {}).map(([agent, state]) => {
                const st = state as { status?: string; summary?: string; root_cause_summary?: string; challenges?: unknown[]; ai_output_hash?: string };
                return (
                  <div key={agent} className="flex items-start gap-2">
                    <div className={`w-6 h-6 rounded-full flex items-center justify-center text-[10px] ${
                      st.status === 'completed' ? 'bg-success-50 text-success-600' :
                      st.status === 'running' ? 'bg-brand-50 text-brand-600 animate-pulse-dot' :
                      'bg-ink-100 text-ink-500'
                    }`}>
                      {st.status === 'completed' ? '✓' : st.status === 'running' ? '●' : '○'}
                    </div>
                    <div className="flex-1">
                      <div className="text-xs font-medium text-ink-800">{agent}</div>
                      {st.summary && <div className="text-xs text-ink-600 mt-0.5">{st.summary}</div>}
                      {st.root_cause_summary && (
                        <div className="text-xs text-ink-700 mt-1 p-2 bg-ink-50 rounded">
                          {st.root_cause_summary}
                        </div>
                      )}
                      {st.challenges && st.challenges.length > 0 && (
                        <div className="text-xs text-ink-600 mt-1">
                          生成 {st.challenges.length} 条反例挑战
                        </div>
                      )}
                      {st.ai_output_hash && (
                        <div className="text-[10px] text-ink-400 font-mono mt-1">
                          输出 hash: {shortHash(st.ai_output_hash)}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

// ---- 报告草稿 ----
function ReportTab({ detail }: { detail: DeviationDetail }) {
  const report = generateReport(detail);
  return (
    <div className="max-w-3xl mx-auto">
      <Card>
        <CardHeader>
          <CardTitle>偏差调查报告（草稿）</CardTitle>
          <Badge variant="brand">ICH Q9 模板</Badge>
        </CardHeader>
        <CardBody>
          <div className="mb-2 text-[10px] text-warning-600">
            前端模板拼装的预览草稿 — 正式报告以 Agent Drafter 产出（Agent 运行 Tab）并经 HITL/签发的版本为准。
          </div>
          <pre className="text-xs text-ink-700 whitespace-pre-wrap font-sans leading-relaxed">
            {report}
          </pre>
        </CardBody>
      </Card>
    </div>
  );
}

function generateReport(d: DeviationDetail): string {
  return `偏差调查报告 - ${d.id}

1. 偏差描述
${d.description || '—'}

2. 严重等级: ${severityLabel(d.severity).toUpperCase()}
3. 发现时间: ${formatDateTime(d.discovered_at)}
4. 影响产品: ${d.product_impact || '待评估'}
5. 发现地点: ${d.area || d.location || '—'}

6. 立即措施
${d.immediate_action || '无'}

7. 根本原因分析（5 Whys）
${d.five_whys.map((w) => `  Why #${w.n}: ${w.question}\n    → ${w.answer}`).join('\n')}

8. Skeptic 反例审阅
${d.skeptic_challenges.length === 0 ? '无' : d.skeptic_challenges.map((c) => `  - ${c.challenge}`).join('\n')}

9. 证据清单
共 ${d.evidence.length} 条证据，覆盖 ${new Set(d.evidence.map((e) => e.source_system)).size} 个源系统

10. 合规声明
本调查遵循 ICH Q9 质量风险管理原则，所有结论均有证据引用支持。
`;
}

function EmptyTab({ text, action }: { text: string; action?: React.ReactNode }) {
  return (
    <Card className="max-w-md mx-auto">
      <CardBody className="text-center py-12">
        <div className="text-ink-400 text-sm mb-3">{text}</div>
        {action}
      </CardBody>
    </Card>
  );
}

/* ============================================
   RCA Pilot — Agent Simulation Engine
   6 Agents with streaming behavior, HITL gates
   ============================================ */

window.AgentEngine = (function () {

  // Agent definitions
  const agentDefs = [
    {
      id: 'meta',
      name: 'Meta-Controller',
      icon: 'cpu',
      subtitle: '任务路由 · 状态机 · 冲突仲裁',
      color: 'brand',
      role: '规划',
    },
    {
      id: 'evidence',
      name: 'Evidence Hunter',
      icon: 'search',
      subtitle: '跨 6 系统只读取证 · 时间线组织',
      color: 'ems',
      role: '取证',
    },
    {
      id: 'reasoner',
      name: 'Reasoner',
      icon: 'brain',
      subtitle: '鱼骨图 6M · 5 Whys · 三角验证',
      color: 'lims',
      role: '推理',
    },
    {
      id: 'skeptic',
      name: 'Skeptic',
      icon: 'siren',
      subtitle: '反例挑战 · 防确认偏误',
      color: 'danger',
      role: '质疑',
    },
    {
      id: 'drafter',
      name: 'Drafter',
      icon: 'pen-line',
      subtitle: 'ICH Q9 报告草稿 · 版本 diff',
      color: 'success',
      role: '起草',
    },
    {
      id: 'guardian',
      name: 'Guardian',
      icon: 'shield-check',
      subtitle: 'ALCOA+ · 21 CFR Part 11 · 审计',
      color: 'warning',
      role: '合规',
    },
  ];

  // Stream scripts per agent — what they "say" while running
  const streamScripts = {
    meta: [
      '解析用户意图…',
      '构建任务 DAG: [取证 → 推理 → 质疑 → 起草 → 合规]',
      '路由到 Evidence Hunter…',
      '监控 Blackboard 状态…',
      '协调 Agent 间通信…',
    ],
    evidence: [
      '查询 LIMS (检验数据)…',
      '查询 MES (批记录)…',
      '查询 EMS (环境监控)…',
      '查询 CMMS (维护工单)…',
      '查询 LMS (培训资质)…',
      '查询 BMS (HVAC 差压)…',
      '组织时间线，计算证据 hash…',
      '已拉取 12 条证据，0 条幻觉',
    ],
    reasoner: [
      '加载 Blackboard 事实库…',
      '构建鱼骨图 6M 分类…',
      '执行 5 Whys 迭代…',
      'Why 1: 人体来源菌进入 B 级区',
      'Why 2: 更衣合规性下降 (CCTV ×4)',
      'Why 3: 资质逾期缺再培训',
      'Why 4: LMS 无告警 → 系统无联动',
      'Why 5: 架构设计缺资质门控',
      '三角验证: 每条结论 ≥2 独立来源 ✓',
    ],
    skeptic: [
      '检索 Blackboard 冲突证据…',
      '检索 Memory Bank 相似案例…',
      '发现反例 SK-1: 8/8 同菌但更衣正常',
      '发现反例 SK-2: Act-08/15 未超标',
      '生成质问: "这能解释 X 吗?"',
      '已提交 2 条反例挑战，等待人工审阅',
    ],
    drafter: [
      '加载 ICH Q9 报告模板…',
      '填充第 1 节: 偏差概述…',
      '填充第 4 节: 根本原因分析…',
      '填充第 6 节: CAPA 建议…',
      '标注证据引用 [E-EMS-05, E-LIMS-01…]…',
      '生成报告草稿 v2',
    ],
    guardian: [
      '扫描 ALCOA+ 9 项原则…',
      'Attributable ✓  Legible ✓  Contemporaneous ✓',
      'Original ✓  Accurate ✓  Complete ✓',
      'Consistent ✓  Enduring ✓  Available ✓',
      '检测: PCR 报告 (E-LIMS-01) 未签收',
      '阻断 0 项违规 · 记录 14 条审计日志',
    ],
  };

  // ---- Simulation state ----
  let state = {
    agents: [],
    running: false,
    paused: false,
    timers: {},
    currentStep: 0,
    onComplete: null,
  };

  function initAgents() {
    state.agents = agentDefs.map(def => ({
      ...def,
      status: 'idle', // idle | running | done | challenging | blocked
      progress: 0,
      streamText: '',
      streamIndex: 0,
    }));
    return state.agents;
  }

  function resetAgents() {
    Object.values(state.timers).forEach(t => clearInterval(t));
    state.timers = {};
    state.running = false;
    state.paused = false;
    state.currentStep = 0;
    state.agents.forEach(a => {
      a.status = 'idle';
      a.progress = 0;
      a.streamText = '';
      a.streamIndex = 0;
    });
    return state.agents;
  }

  // ---- Streaming text typewriter ----
  function streamText(agent, fullText, onDone) {
    let i = 0;
    agent.streamText = '';
    if (state.timers[agent.id]) clearInterval(state.timers[agent.id]);
    state.timers[agent.id] = setInterval(() => {
      if (state.paused) return;
      if (i >= fullText.length) {
        clearInterval(state.timers[agent.id]);
        delete state.timers[agent.id];
        if (onDone) onDone();
        return;
      }
      agent.streamText += fullText[i];
      i++;
    }, 18);
  }

  // ---- Run a single agent's full script ----
  function runAgent(agent, scripts, onAgentDone) {
    agent.status = 'running';
    agent.progress = 5;
    let scriptIdx = 0;

    function nextScript() {
      if (scriptIdx >= scripts.length) {
        agent.status = 'done';
        agent.progress = 100;
        agent.streamText = scripts[scripts.length - 1] || '完成';
        if (onAgentDone) onAgentDone();
        return;
      }
      if (state.paused) {
        setTimeout(nextScript, 200);
        return;
      }
      const text = scripts[scriptIdx];
      streamText(agent, text, () => {
        scriptIdx++;
        agent.progress = Math.round((scriptIdx / scripts.length) * 100);
        setTimeout(nextScript, 250);
      });
    }
    nextScript();
  }

  // ---- Full pipeline run ----
  // Sequence: meta → evidence → reasoner → skeptic → drafter → guardian
  function runPipeline(agents, callbacks) {
    if (state.running) return;
    state.running = true;
    state.paused = false;
    state.onComplete = callbacks && callbacks.onComplete;

    const order = ['meta', 'evidence', 'reasoner', 'skeptic', 'drafter', 'guardian'];
    let idx = 0;

    function next() {
      if (idx >= order.length) {
        state.running = false;
        if (state.onComplete) state.onComplete();
        return;
      }
      if (!state.running) return; // reset
      const agentId = order[idx];
      const agent = agents.find(a => a.id === agentId);
      if (!agent) { idx++; next(); return; }

      // Skeptic special: shows challenge status
      if (agentId === 'skeptic') {
        runAgent(agent, streamScripts.skeptic, () => {
          // After skeptic, trigger challenge modal
          if (callbacks && callbacks.onSkepticChallenge) {
            agent.status = 'challenging';
            callbacks.onSkepticChallenge();
          }
          idx++;
          setTimeout(next, 600);
        });
      } else {
        runAgent(agent, streamScripts[agentId] || [], () => {
          idx++;
          setTimeout(next, 400);
        });
      }
    }
    next();
  }

  function pausePipeline() {
    state.paused = true;
    state.agents.forEach(a => { if (a.status === 'running') a.status = 'blocked'; });
  }

  function resumePipeline() {
    state.paused = false;
    state.agents.forEach(a => { if (a.status === 'blocked') a.status = 'running'; });
  }

  function isRunning() { return state.running; }

  // ---- HITL gate helpers ----
  const hitlGates = [
    { id: 'gate1', label: '证据时间线确认', agent: 'evidence', desc: '调查员确认证据完整或补充', required: true },
    { id: 'gate2', label: '5 Whys 逐条确认', agent: 'reasoner', desc: '每个 Why 接受/修改/拒绝', required: true },
    { id: 'gate3', label: 'Skeptic 反例审阅', agent: 'skeptic', desc: '必须审阅，不可跳过', required: true },
    { id: 'gate4', label: '报告草稿逐段确认', agent: 'drafter', desc: '编辑自动记录', required: true },
    { id: 'gate5', label: '合规扫描 + 电子签名', agent: 'guardian', desc: '提交前最终检查', required: true },
  ];

  return {
    agentDefs,
    initAgents,
    resetAgents,
    runPipeline,
    pausePipeline,
    resumePipeline,
    isRunning,
    hitlGates,
    getState: () => state,
  };
})();

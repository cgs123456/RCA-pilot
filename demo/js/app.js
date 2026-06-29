/* ============================================
   RCA Pilot — Main Application (Alpine.js)
   State · Routing · 8 Page Renderers · HITL
   ============================================ */

function rcaApp() {
  return {
    // ---- State ----
    currentPage: 'dashboard',
    navCollapsed: false,
    copilotCollapsed: false,
    activeCaseId: 'DEV-2025-08-147',
    investigationTab: 'timeline',
    agents: [],
    agentsRunning: false,
    commandBarOpen: false,
    commandQuery: '',
    filteredCommands: [],
    skepticModal: { open: false, target: '', challenge: '', evidence: [], currentIndex: 0 },
    evidenceModal: { open: false, data: null },
    notifPanelOpen: false,
    toasts: [],
    auditMini: { summary: '已引用 12 条证据 · 3 次 AI 修订', recent: [] },

    // ---- Data references ----
    navItems: [
      { id: 'n1', page: 'dashboard',     label: '仪表盘',     icon: 'layout-dashboard', badge: '', badgeType: '' },
      { id: 'n2', page: 'inbox',         label: '调查收件箱', icon: 'inbox',            badge: '4', badgeType: 'brand' },
      { id: 'n3', page: 'investigation', label: '调查工作台', icon: 'microscope',       badge: '', badgeType: '' },
      { id: 'n4', page: 'evidence',      label: '证据库',     icon: 'folder-search',    badge: '', badgeType: '' },
      { id: 'n5', page: 'report',        label: '报告预览',   icon: 'file-text',        badge: '', badgeType: '' },
      { id: 'n6', page: 'audit',         label: '审计追踪',   icon: 'shield',           badge: '', badgeType: '' },
      { id: 'n7', page: 'knowledge',     label: '知识库',     icon: 'brain-circuit',    badge: 'β', badgeType: 'brand' },
      { id: 'n8', page: 'admin',         label: '管理与合规', icon: 'settings',         badge: '', badgeType: '' },
    ],

    // ============================================================
    //  Init
    // ============================================================
    init() {
      window.app = this; // global reference for onclick handlers in x-html
      this.agents = AgentEngine.initAgents();
      this.filteredCommands = MockData.commands;
      this.refreshAuditMini();

      // Keyboard shortcut for command bar
      window.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
          e.preventDefault();
          this.openCommandBar();
        }
      });

      // Initial render hooks
      this.$watch('currentPage', (val) => this.handlePageRender(val));
      this.$watch('investigationTab', () => this.handleInvestigationTabRender());

      this.$nextTick(() => {
        RCACharts.setupChartDefaults();
        this.handlePageRender(this.currentPage);
      });

      // Toast: welcome
      setTimeout(() => this.toast('RCA Pilot 已就绪 · 当前案例 DEV-2025-08-147', 'brand', 'sparkles'), 400);
    },

    get activeCase() {
      return MockData.getCase(this.activeCaseId);
    },
    get pageTitle() {
      const map = { dashboard: '仪表盘', inbox: '调查收件箱', investigation: '调查工作台', evidence: '证据库', report: '报告预览', audit: '审计追踪', knowledge: '知识库', admin: '管理与合规' };
      return map[this.currentPage] || 'RCA Pilot';
    },

    // ============================================================
    //  Navigation
    // ============================================================
    navigate(page, caseId) {
      if (caseId) this.activeCaseId = caseId;
      this.currentPage = page;
      this.notifPanelOpen = false;
      this.commandBarOpen = false;
    },

    handlePageRender(page) {
      this.$nextTick(() => {
        if (typeof lucide !== 'undefined') lucide.createIcons();
        if (page === 'dashboard') this.renderDashboardCharts();
        if (page === 'investigation') this.handleInvestigationTabRender();
        if (page === 'evidence') this.renderEvidenceCharts();
        if (page === 'knowledge') this.renderKnowledgeCharts();
        if (page === 'audit') this.renderAuditCharts();
      });
    },

    handleInvestigationTabRender() {
      if (this.currentPage !== 'investigation') return;
      this.$nextTick(() => {
        if (typeof lucide !== 'undefined') lucide.createIcons();
        const tab = this.investigationTab;
        if (tab === 'fishbone' && this.activeCase?.fishbone) {
          RCACharts.renderFishbone('fishbone-container', this.activeCase.fishbone, (cause) => {
            if (cause.evidence && cause.evidence.length) {
              this.openEvidence(cause.evidence[0]);
            }
          });
        }
        if (tab === 'topology' && this.activeCase?.topology) {
          RCACharts.renderTopology('topology-container', this.activeCase.topology, (node) => {
            if (node.type === 'evidence') this.openEvidence(node.id);
          });
        }
        if (tab === 'timeline' || tab === 'evidence') {
          this.renderTrendChart();
        }
      });
    },

    renderTrendChart() {
      this.$nextTick(() => {
        if (this.activeCase?.emTrend && document.getElementById('em-trend-chart')) {
          RCACharts.renderEMTrend('em-trend-chart', this.activeCase.emTrend);
        }
        if (this.activeCase?.hvacTrend && document.getElementById('hvac-trend-chart')) {
          RCACharts.renderHVACTrend('hvac-trend-chart', this.activeCase.hvacTrend);
        }
      });
    },

    // ============================================================
    //  Command Bar
    // ============================================================
    openCommandBar() {
      this.commandBarOpen = true;
      this.commandQuery = '';
      this.filteredCommands = MockData.commands;
      this.$nextTick(() => { if (this.$refs.cmdInput) this.$refs.cmdInput.focus(); });
    },
    filterCommands() {
      const q = this.commandQuery.toLowerCase().trim();
      if (!q) { this.filteredCommands = MockData.commands; return; }
      this.filteredCommands = MockData.commands.filter(c =>
        c.title.toLowerCase().includes(q) || c.subtitle.toLowerCase().includes(q) || (c.action && c.action.caseId && c.action.caseId.toLowerCase().includes(q))
      );
    },
    executeCommand(cmd) {
      this.commandBarOpen = false;
      if (cmd.action.fn === 'newDeviation') {
        this.toast('新建偏差调查功能 (Demo)', 'brand', 'plus-circle');
        return;
      }
      this.navigate(cmd.action.page, cmd.action.caseId);
    },

    // ============================================================
    //  Agents
    // ============================================================
    runAgents() {
      if (this.agentsRunning) return;
      this.agentsRunning = true;
      this.agents = AgentEngine.initAgents();
      const self = this;
      AgentEngine.runPipeline(this.agents, {
        onSkepticChallenge() {
          // Show first skeptic challenge
          if (self.activeCase?.skepticChallenges?.length) {
            const ch = self.activeCase.skepticChallenges[self.skepticModal.currentIndex] || self.activeCase.skepticChallenges[0];
            self.skepticModal.target = ch.target;
            self.skepticModal.challenge = ch.challenge;
            self.skepticModal.evidence = ch.evidence;
            self.skepticModal.currentIndex = 0;
            self.skepticModal.open = true;
          }
        },
        onComplete() {
          self.agentsRunning = false;
          self.toast('全部 Agent 执行完成 · 报告 v2 已生成', 'success', 'check-circle');
          self.addAuditLog('AGENT_PIPELINE', 'all agents', '6 Agent 协作完成', []);
          self.refreshAuditMini();
        },
      });
      this.toast('Agent 流水线已启动…', 'brand', 'play');
    },
    pauseAgents() {
      AgentEngine.pausePipeline();
      this.toast('Agent 已暂停', 'warning', 'pause');
    },
    resetAgents() {
      AgentEngine.resetAgents();
      this.agents = AgentEngine.initAgents();
      this.agentsRunning = false;
      this.toast('Agent 已重置', 'brand', 'rotate-ccw');
    },

    // ============================================================
    //  Skeptic / HITL
    // ============================================================
    dismissSkeptic(action) {
      const ch = this.activeCase.skepticChallenges[this.skepticModal.currentIndex];
      if (action === 'accept') {
        this.addAuditLog('ACCEPT_SKEPTIC', `SK-${this.skepticModal.currentIndex + 1}`, '采纳反例，触发 Reasoner 重新推理', ch.evidence.map(e => e.id));
        this.toast('反例已采纳 · Reasoner 重新推理中…', 'danger', 'refresh-cw');
        // Mark next why as pending re-reason
        const idx = this.skepticModal.currentIndex + 1;
        if (idx < this.activeCase.skepticChallenges.length) {
          this.skepticModal.currentIndex = idx;
          const next = this.activeCase.skepticChallenges[idx];
          this.skepticModal.target = next.target;
          this.skepticModal.challenge = next.challenge;
          this.skepticModal.evidence = next.evidence;
        } else {
          this.skepticModal.open = false;
        }
      } else {
        this.addAuditLog('REJECT_SKEPTIC', `SK-${this.skepticModal.currentIndex + 1}`, '驳回反例', []);
        this.toast('反例已驳回', 'brand', 'x-circle');
        const idx = this.skepticModal.currentIndex + 1;
        if (idx < this.activeCase.skepticChallenges.length) {
          this.skepticModal.currentIndex = idx;
          const next = this.activeCase.skepticChallenges[idx];
          this.skepticModal.target = next.target;
          this.skepticModal.challenge = next.challenge;
          this.skepticModal.evidence = next.evidence;
        } else {
          this.skepticModal.open = false;
        }
      }
      this.refreshAuditMini();
    },

    acceptWhy(n) {
      if (!this.activeCase) return;
      const why = this.activeCase.fiveWhys.find(w => w.n === n);
      if (why) { why.status = 'accepted'; this.addAuditLog('ACCEPT_AI_OUTPUT', `5WHY-${n}`, `接受 Why ${n}`, why.evidence); this.toast(`Why ${n} 已接受`, 'success', 'check'); this.refreshAuditMini(); }
      this.forceReRender();
    },
    rejectWhy(n) {
      const why = this.activeCase.fiveWhys.find(w => w.n === n);
      if (why) { why.status = 'rejected'; this.addAuditLog('REJECT_AI_OUTPUT', `5WHY-${n}`, `拒绝 Why ${n} (原因: 推理不足)`, []); this.toast(`Why ${n} 已拒绝 — 请填写原因`, 'warning', 'x'); }
      this.forceReRender();
    },
    editWhy(n) {
      const why = this.activeCase.fiveWhys.find(w => w.n === n);
      if (why) { why.status = 'edited'; this.addAuditLog('EDIT_5WHY', `5WHY-${n}`, `修改 Why ${n}`, why.evidence); this.toast(`Why ${n} 进入编辑模式`, 'brand', 'edit-3'); }
      this.forceReRender();
    },

    // Force re-render of x-html content (mutated MockData is not reactive)
    forceReRender() {
      const cur = this.currentPage;
      this.currentPage = '__tmp__';
      this.$nextTick(() => { this.currentPage = cur; this.handlePageRender(cur); });
    },

    // ============================================================
    //  Evidence modal
    // ============================================================
    openEvidence(evidenceId) {
      // Search across all cases
      let ev = null;
      for (const c of MockData.cases) {
        if (c.evidence) {
          ev = c.evidence.find(e => e.id === evidenceId);
          if (ev) break;
        }
      }
      if (ev) {
        this.evidenceModal.data = ev;
        this.evidenceModal.open = true;
      } else {
        this.toast('证据 ' + evidenceId + ' 不存在', 'warning', 'alert-triangle');
      }
    },

    // ============================================================
    //  Notifications
    // ============================================================
    toggleNotifications() { this.notifPanelOpen = !this.notifPanelOpen; },
    handleNotifClick(notif) {
      this.notifPanelOpen = false;
      if (notif.action) this.navigate(notif.action.page, notif.action.caseId);
    },
    get notifications() { return MockData.notifications; },

    // ============================================================
    //  Toast
    // ============================================================
    toast(message, type, icon) {
      const id = 'T' + Date.now() + Math.random().toString(36).slice(2, 6);
      this.toasts.push({ id, message, type: type || 'brand', icon: icon || 'info' });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 3200);
    },

    // ============================================================
    //  Audit
    // ============================================================
    addAuditLog(action, target, detail, evidenceRefs) {
      // For demo — just refresh mini panel from mock data
      this.refreshAuditMini();
    },
    refreshAuditMini() {
      const recent = MockData.auditLogs.slice(-4).reverse().map(l => ({
        id: l.log_id,
        action: l.action.replace(/_/g, ' ') + ' · ' + (l.target || ''),
        time: l.time.split('T')[1].slice(0, 5),
        severity: l.action.includes('CHALLENGE') ? 'danger' : l.action.includes('EDIT') ? 'warning' : 'brand',
      }));
      this.auditMini.recent = recent;
      this.auditMini.summary = `已引用 ${this.activeCase?.evidence?.length || 0} 条证据 · 3 次 AI 修订`;
    },

    // ============================================================
    //  Helper classes
    // ============================================================
    severityDotClass(sev) {
      return { critical: 'dot-critical', major: 'dot-major', minor: 'dot-minor', info: 'dot-info' }[sev] || 'dot-info';
    },
    severityBadgeClass(sev) {
      return { critical: 'sev-critical', major: 'sev-critical', minor: 'sev-minor', info: 'sev-info' }[sev] || 'sev-info';
    },
    severityLabel(sev) {
      return { critical: '严重 Critical', major: '重大 Major', minor: '一般 Minor', info: '提示 Info' }[sev] || sev;
    },
    statusBadgeClass(status) {
      return {
        investigating: 'bg-brand-50 text-brand-700 border border-brand-200',
        reviewing: 'bg-warning-50 text-warning-700 border border-warning-200',
        pending: 'bg-ink-100 text-ink-600 border border-line',
        closed: 'bg-success-50 text-success-700 border border-success-200',
      }[status] || 'bg-ink-100 text-ink-600 border border-line';
    },
    statusLabel(status) {
      return {
        investigating: '调查中',
        reviewing: 'QA 审核',
        pending: '待处理',
        closed: '已关闭',
      }[status] || status;
    },
    sourceColorClass(source) {
      const s = MockData.sourceSystems[source];
      return s ? s.color : 'src-lims';
    },
    sourceIcon(source) {
      const s = MockData.sourceSystems[source];
      return s ? s.icon : 'file';
    },
    sourceLabel(source) {
      const s = MockData.sourceSystems[source];
      return s ? s.label : source;
    },
    confidenceLabel(c) {
      return { 4: '高', 3: '中高', 2: '中', 1: '低' }[c] || '中';
    },
    confidenceLabelClass(c) {
      return { 4: 'text-success-600', 3: 'text-brand-600', 2: 'text-warning-600', 1: 'text-danger-600' }[c] || 'text-ink-500';
    },
    agentColorClass(agent) {
      const map = { brand: 'bg-brand-50 text-brand-600', ems: 'bg-warning-50 text-warning-600', lims: 'bg-brand-50 text-brand-600', danger: 'bg-danger-50 text-danger-600', success: 'bg-success-50 text-success-600', warning: 'bg-warning-50 text-warning-600' };
      return map[agent.color] || 'bg-ink-100 text-ink-700';
    },
    agentStatusBadgeClass(status) {
      return { idle: 'bg-ink-100 text-ink-500', running: 'bg-brand-50 text-brand-600', done: 'bg-success-50 text-success-600', challenging: 'bg-danger-50 text-danger-600', blocked: 'bg-warning-50 text-warning-600' }[status] || 'bg-ink-100 text-ink-500';
    },
    agentStatusLabel(status) {
      return { idle: '等待', running: '运行中', done: '完成', challenging: '挑战中', blocked: '已暂停' }[status] || status;
    },

    // ============================================================
    //  PAGE RENDERERS
    // ============================================================

    // ---- P1: Dashboard ----
    renderDashboard() {
      const d = MockData.dashboard;
      const colorMap = {
        brand:   { bg: '#EEF4FF', text: '#0A52E0' },
        danger:  { bg: '#FDECEE', text: '#B81E2E' },
        success: { bg: '#E6F4F0', text: '#0B6553' },
        warning: { bg: '#FEF3E2', text: '#D98106' },
      };
      const kpiCards = d.kpis.map(k => {
        const cm = colorMap[k.color] || colorMap.brand;
        return `
        <div class="bg-panel rounded-xl border border-line p-4 card-hover">
          <div class="flex items-start justify-between">
            <div class="w-9 h-9 rounded-lg flex items-center justify-center" style="background:${cm.bg};color:${cm.text}">
              <i data-lucide="${k.icon}" class="w-4.5 h-4.5"></i>
            </div>
            <div class="flex items-center gap-1 text-[11px] font-medium ${k.trendBad ? 'text-danger-600' : k.trend === 'down' ? 'text-success-600' : 'text-warning-600'}">
              <i data-lucide="${k.trend === 'down' ? 'arrow-down-right' : 'arrow-up-right'}" class="w-3 h-3"></i>
              <span>${k.delta}${k.unit ? k.unit : ''}</span>
            </div>
          </div>
          <div class="mt-3">
            <div class="text-[28px] font-bold text-ink-900 leading-none">${k.value}<span class="text-[14px] text-ink-500 font-normal ml-1">${k.unit || ''}</span></div>
            <div class="text-[12px] text-ink-500 mt-1">${k.label} · <span class="text-ink-300">${k.deltaLabel}</span></div>
          </div>
        </div>
      `;}).join('');

      const todoItems = d.todos.map(t => `
        <div class="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-line hover:bg-ink-100/40 cursor-pointer transition-colors" onclick="app.navigate('investigation','${t.caseId}')">
          <span class="w-1.5 h-1.5 rounded-full ${t.severity === 'critical' ? 'bg-danger-500' : t.severity === 'major' ? 'bg-danger-500' : 'bg-warning-500'}"></span>
          <div class="flex-1 min-w-0">
            <div class="text-[12px] font-medium text-ink-900 truncate">${t.title}</div>
            <div class="text-[10px] text-ink-500 font-mono">${t.caseId} · ${t.type}</div>
          </div>
          <div class="text-[10px] text-ink-500 shrink-0">${t.due}</div>
        </div>
      `).join('');

      const activityItems = d.activities.map(a => `
        <div class="flex items-center gap-2.5 py-2">
          <div class="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-semibold shrink-0 ${a.avatar === 'AI' ? 'bg-gradient-to-br from-brand-500 to-brand-700 text-white' : 'bg-gradient-to-br from-ink-300 to-ink-500 text-white'}">${a.avatar}</div>
          <div class="flex-1 text-[12px]">
            <span class="font-medium text-ink-900">${a.user}</span>
            <span class="text-ink-500"> ${a.action} </span>
            <span class="font-medium text-brand-600">${a.target}</span>
          </div>
          <div class="text-[10px] text-ink-300 font-mono shrink-0">${a.time}</div>
        </div>
      `).join('');

      return `
        <div class="p-6 space-y-5 max-w-[1400px] mx-auto pb-24">
          <!-- Header -->
          <div class="flex items-end justify-between">
            <div>
              <h1 class="text-[20px] font-bold text-ink-900">质量偏差概览</h1>
              <p class="text-[12px] text-ink-500 mt-0.5">2025年8月 · 全厂偏差趋势与根因模式</p>
            </div>
            <div class="flex items-center gap-2">
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5"><i data-lucide="calendar" class="w-3.5 h-3.5"></i>近 30 天</button>
              <button class="px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-[12px] font-medium flex items-center gap-1.5" onclick="app.navigate('investigation')"><i data-lucide="plus" class="w-3.5 h-3.5"></i>新建调查</button>
            </div>
          </div>

          <!-- KPI cards -->
          <div class="grid grid-cols-4 gap-4">${kpiCards}</div>

          <!-- Charts row -->
          <div class="grid grid-cols-3 gap-4">
            <div class="col-span-2 bg-panel rounded-xl border border-line p-4">
              <div class="flex items-center justify-between mb-3">
                <div>
                  <h3 class="text-[13px] font-semibold text-ink-900">偏差趋势 (按部门)</h3>
                  <p class="text-[11px] text-ink-500">近 30 天 · 堆叠柱状图</p>
                </div>
                <div class="flex gap-1 text-[11px]">
                  <button class="px-2 py-1 rounded bg-brand-50 text-brand-600 font-medium">30天</button>
                  <button class="px-2 py-1 rounded text-ink-500 hover:bg-ink-100">90天</button>
                  <button class="px-2 py-1 rounded text-ink-500 hover:bg-ink-100">年度</button>
                </div>
              </div>
              <div style="height:240px;"><canvas id="dashboard-trend-chart"></canvas></div>
            </div>
            <div class="bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[13px] font-semibold text-ink-900 mb-1">热力图</h3>
              <p class="text-[11px] text-ink-500 mb-3">房间 × 偏差类型</p>
              <div id="dashboard-heatmap"></div>
            </div>
          </div>

          <!-- Bottom row -->
          <div class="grid grid-cols-3 gap-4">
            <!-- Todos -->
            <div class="bg-panel rounded-xl border border-line p-4">
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-[13px] font-semibold text-ink-900">我的待办</h3>
                <span class="text-[10px] px-2 py-0.5 rounded-full bg-danger-50 text-danger-600 font-semibold">${d.todos.length}</span>
              </div>
              <div class="space-y-1.5">${todoItems}</div>
            </div>
            <!-- Activities -->
            <div class="col-span-2 bg-panel rounded-xl border border-line p-4">
              <div class="flex items-center justify-between mb-2">
                <h3 class="text-[13px] font-semibold text-ink-900">最近活动</h3>
                <button class="text-[11px] text-brand-600 hover:text-brand-700 font-medium" onclick="app.navigate('audit')">查看全部 →</button>
              </div>
              <div class="divide-y divide-line/60">${activityItems}</div>
            </div>
          </div>
        </div>
      `;
    },

    renderDashboardCharts() {
      this.$nextTick(() => {
        RCACharts.renderDeviationTrend('dashboard-trend-chart', MockData.dashboard.deviationTrend);
        RCACharts.renderHeatmap('dashboard-heatmap', MockData.dashboard.heatmap, (cell) => {
          this.toast(`${cell.room} · ${cell.type}: ${cell.value} 次`, 'brand', 'grid-3x3');
        });
      });
    },

    // ---- P2: Inbox ----
    renderInbox() {
      const items = MockData.inboxItems.map(item => {
        const systems = item.systems.map(s => `<span class="text-[9px] font-mono px-1 py-0.5 rounded ${MockData.sourceSystems[s]?.color || 'src-lims'}" title="${MockData.sourceSystems[s]?.label || s}">${s}</span>`).join('');
        return `
          <div class="flex items-center gap-3 px-4 py-3 border-b border-line hover:bg-brand-50/30 cursor-pointer transition-colors" onclick="app.navigate('investigation','${item.id}')">
            <div class="w-1 h-10 rounded-full ${item.severity === 'major' ? 'bg-danger-500' : 'bg-warning-500'}"></div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="text-[12px] font-mono font-semibold text-ink-700">${item.id}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded font-medium ${this.severityBadgeClass(item.severity)}">${this.severityLabel(item.severity)}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded font-medium ${this.statusBadgeClass(item.status)}">${this.statusLabel(item.status)}</span>
                <span class="text-[10px] text-ink-500">${item.dept}</span>
              </div>
              <div class="text-[13px] font-medium text-ink-900 mt-1 truncate">${item.title}</div>
              <div class="flex items-center gap-3 mt-1.5 text-[10px] text-ink-500">
                <span><i data-lucide="user" class="w-3 h-3 inline"></i> ${item.investigator}</span>
                <span><i data-lucide="calendar" class="w-3 h-3 inline"></i> ${item.discoveredAt}</span>
                <div class="flex gap-0.5">${systems}</div>
              </div>
            </div>
            <div class="flex flex-col items-end gap-1.5 shrink-0">
              <div class="text-[11px] font-mono ${item.daysLeft === 0 ? 'text-danger-600' : 'text-ink-700'}">${item.daysLeft === 0 ? '已关闭' : '剩余 ' + item.daysLeft + ' 天'}</div>
              <div class="w-20 h-1.5 bg-ink-100 rounded-full overflow-hidden"><div class="h-full ${item.progress === 100 ? 'bg-success-500' : 'bg-brand-500'}" style="width:${item.progress}%"></div></div>
              <div class="text-[10px] text-ink-500">${item.progress}%</div>
            </div>
          </div>
        `;
      }).join('');

      return `
        <div class="p-6 max-w-[1400px] mx-auto pb-24">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-bold">调查收件箱</h1>
              <p class="text-[12px] text-ink-500 mt-0.5">智能排序: 严重度(0.6) × 剩余时间(0.4) · ${MockData.inboxItems.length} 项</p>
            </div>
            <div class="flex gap-2">
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5"><i data-lucide="filter" class="w-3.5 h-3.5"></i>筛选</button>
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5"><i data-lucide="users" class="w-3.5 h-3.5"></i>批量分配</button>
            </div>
          </div>
          <!-- Filters -->
          <div class="flex items-center gap-2 mb-3 text-[12px]">
            <button class="px-3 py-1.5 rounded-lg bg-brand-500 text-white font-medium">全部 ${MockData.inboxItems.length}</button>
            <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-ink-700">调查中 2</button>
            <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-ink-700">QA 审核 1</button>
            <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-ink-700">已关闭 3</button>
            <div class="flex-1"></div>
            <span class="text-ink-500">排序:</span>
            <select class="text-[12px] border border-line rounded-lg px-2 py-1.5 bg-panel"><option>智能优先级</option><option>截止日期</option><option>严重度</option></select>
          </div>
          <!-- List -->
          <div class="bg-panel rounded-xl border border-line overflow-hidden">${items}</div>
        </div>
      `;
    },

    // ---- P3: Investigation Workbench ----
    renderInvestigation() {
      const c = this.activeCase;
      if (!c) return '<div class="p-8 text-center text-ink-500">未选择案例</div>';

      const tabs = [
        { id: 'timeline',  label: '① 时间线',    icon: 'git-commit-vertical' },
        { id: 'evidence',  label: '② 证据',      icon: 'folder-search' },
        { id: 'fishbone',  label: '③ 鱼骨图',    icon: 'git-fork' },
        { id: 'whys',      label: '④ 5 Whys',   icon: 'list-ordered' },
        { id: 'topology',  label: '⑤ 证据拓扑',  icon: 'share-2' },
        { id: 'report',    label: '⑥ 报告草稿',  icon: 'file-text' },
      ];

      const tabButtons = tabs.map(t => `
        <button onclick="app.investigationTab='${t.id}'; app.handleInvestigationTabRender()" class="flex items-center gap-1.5 px-3 py-2 text-[12px] font-medium border-b-2 transition-colors ${this.investigationTab === t.id ? 'tab-active' : 'border-transparent text-ink-500 hover:text-ink-700'}">
          <i data-lucide="${t.icon}" class="w-3.5 h-3.5"></i>${t.label}
        </button>
      `).join('');

      let content = '';
      if (this.investigationTab === 'timeline') content = this.renderTimelineTab(c);
      else if (this.investigationTab === 'evidence') content = this.renderEvidenceTab(c);
      else if (this.investigationTab === 'fishbone') content = `<div id="fishbone-container" class="p-4 bg-panel"></div>`;
      else if (this.investigationTab === 'whys') content = this.renderWhysTab(c);
      else if (this.investigationTab === 'topology') content = `<div id="topology-container" class="p-4 bg-panel"></div>`;
      else if (this.investigationTab === 'report') content = this.renderReportTab(c);

      return `
        <div class="flex flex-col h-full">
          <!-- Case header -->
          <div class="px-5 py-3 bg-panel border-b border-line shrink-0">
            <div class="flex items-center gap-3">
              <span class="text-[10px] px-2 py-1 rounded font-semibold ${this.severityBadgeClass(c.severity)}">${this.severityLabel(c.severity)}</span>
              <span class="text-[10px] px-2 py-1 rounded font-medium bg-ink-100 text-ink-700">${c.statusLabel}</span>
              <span class="text-[14px] font-mono font-semibold">${c.id}</span>
              <span class="text-[13px] text-ink-700 truncate flex-1">${c.title}</span>
              <div class="flex items-center gap-3 text-[11px] text-ink-500">
                <span><i data-lucide="map-pin" class="w-3 h-3 inline"></i> ${c.location}</span>
                <span><i data-lucide="clock" class="w-3 h-3 inline"></i> 截止 ${c.deadline}</span>
                <span><i data-lucide="user" class="w-3 h-3 inline"></i> ${MockData.getUser(c.investigator)?.name}</span>
              </div>
            </div>
            <!-- HITL progress -->
            <div class="flex items-center gap-2 mt-2.5">
              <span class="text-[10px] text-ink-500 font-medium">HITL 关卡:</span>
              ${[1,2,3,4,5].map(n => {
                const g = c.hitlGates['gate'+n];
                const icon = g === 'done' ? 'check-circle-2' : g === 'pending' ? 'clock' : 'circle';
                const cls = g === 'done' ? 'text-success-600' : g === 'pending' ? 'text-warning-600' : 'text-ink-300';
                return `<span class="flex items-center gap-1 text-[10px] ${cls}"><i data-lucide="${icon}" class="w-3 h-3"></i>${n}</span>`;
              }).join('')}
              <div class="flex-1"></div>
              <span class="text-[10px] text-ink-500">进度 ${c.progress}%</span>
              <div class="w-24 h-1 bg-ink-100 rounded-full overflow-hidden"><div class="h-full bg-brand-500" style="width:${c.progress}%"></div></div>
            </div>
          </div>
          <!-- Tabs -->
          <div class="flex items-center gap-1 px-4 bg-panel border-b border-line shrink-0 overflow-x-auto">${tabButtons}</div>
          <!-- Content -->
          <div class="flex-1 overflow-y-auto bg-canvas">${content}</div>
        </div>
      `;
    },

    renderTimelineTab(c) {
      const events = (c.timeline || []).map(t => `
        <div class="flex gap-3 group">
          <div class="flex flex-col items-center shrink-0 pt-0.5">
            <div class="timeline-dot ${t.anomaly ? 'anomaly' : ''}" style="${t.severity === 'danger' ? 'background:#D72638;box-shadow:0 0 0 1px #D72638' : t.severity === 'warning' ? 'background:#F59E0B' : 'background:#94A3B8'}"></div>
          </div>
          <div class="flex-1 pb-4 min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-[11px] font-mono text-ink-500">${t.time}</span>
              ${t.source && t.source !== '—' ? `<span class="text-[9px] font-mono px-1 py-0.5 rounded ${MockData.sourceSystems[t.source]?.color || ''}">${t.source}</span>` : ''}
              ${t.anomaly ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-danger-50 text-danger-600 font-semibold animate-pulse-dot">异常</span>' : ''}
            </div>
            <div class="text-[13px] font-medium text-ink-900 mt-0.5">${t.title}</div>
            <div class="text-[12px] text-ink-500 mt-0.5">${t.detail}</div>
            ${t.evidenceId ? `<button onclick="app.openEvidence('${t.evidenceId}')" class="mt-1 text-[11px] text-brand-600 hover:text-brand-700 font-mono font-medium flex items-center gap-1"><i data-lucide="external-link" class="w-3 h-3"></i>${t.evidenceId}</button>` : ''}
          </div>
        </div>
      `).join('');

      return `
        <div class="p-5 grid grid-cols-3 gap-5">
          <div class="col-span-2">
            <div class="bg-panel rounded-xl border border-line p-5">
              <div class="flex items-center justify-between mb-4">
                <h3 class="text-[13px] font-semibold">证据时间线</h3>
                <div class="flex gap-1.5">
                  <button class="px-2.5 py-1 rounded border border-line hover:bg-ink-100 text-[11px] text-ink-700 flex items-center gap-1"><i data-lucide="plus" class="w-3 h-3"></i>补充事件</button>
                  <button class="px-2.5 py-1 rounded bg-success-50 text-success-600 text-[11px] font-medium flex items-center gap-1"><i data-lucide="check" class="w-3 h-3"></i>确认完整 (HITL 1)</button>
                </div>
              </div>
              <div class="relative pl-1 timeline-line">${events}</div>
            </div>
          </div>
          <div class="space-y-4">
            <div class="bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[12px] font-semibold mb-2">EM 趋势</h3>
              <div style="height:160px;"><canvas id="em-trend-chart"></canvas></div>
            </div>
            ${c.hvacTrend ? `
            <div class="bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[12px] font-semibold mb-2">AHU-7 差压</h3>
              <div style="height:140px;"><canvas id="hvac-trend-chart"></canvas></div>
            </div>` : ''}
          </div>
        </div>
      `;
    },

    renderEvidenceTab(c) {
      // Group by source
      const sources = ['LIMS','MES','EMS','CMMS','LMS','BMS'];
      const cards = (c.evidence || []).map(ev => {
        const src = MockData.sourceSystems[ev.source];
        return `
          <div class="bg-panel rounded-lg border border-line p-3 card-hover cursor-pointer" onclick="app.openEvidence('${ev.id}')">
            <div class="flex items-center gap-2 mb-2">
              <div class="w-7 h-7 rounded-md flex items-center justify-center ${src?.color || ''}"><i data-lucide="${src?.icon || 'file'}" class="w-3.5 h-3.5"></i></div>
              <span class="text-[11px] font-mono font-semibold text-ink-900">${ev.id}</span>
              <div class="flex gap-0.5 ml-auto">${[1,2,3,4].map(n => `<span class="conf-dot ${n <= ev.confidence ? 'bg-brand-500' : 'bg-ink-300'}"></span>`).join('')}</div>
            </div>
            <div class="text-[12px] text-ink-700 leading-snug line-clamp-2">${ev.summary}</div>
            <div class="flex items-center gap-2 mt-2 text-[10px] text-ink-500 font-mono">
              <span><i data-lucide="clock" class="w-2.5 h-2.5 inline"></i> ${ev.timestamp.split('T')[1].slice(0,5)}</span>
              <span class="truncate"><i data-lucide="hash" class="w-2.5 h-2.5 inline"></i> ${ev.hash.slice(6,18)}</span>
            </div>
          </div>
        `;
      }).join('');

      const sourceStats = sources.map(s => {
        const count = (c.evidence || []).filter(e => e.source === s).length;
        const sys = MockData.sourceSystems[s];
        return `
          <div class="flex items-center gap-2 px-3 py-2 rounded-lg border border-line">
            <div class="w-7 h-7 rounded-md flex items-center justify-center ${sys.color}"><i data-lucide="${sys.icon}" class="w-3.5 h-3.5"></i></div>
            <div class="flex-1 min-w-0">
              <div class="text-[11px] font-medium text-ink-900">${s}</div>
              <div class="text-[9px] text-ink-500 truncate">${sys.desc}</div>
            </div>
            <span class="text-[12px] font-bold ${sys.color}">${count}</span>
          </div>
        `;
      }).join('');

      return `
        <div class="p-5 grid grid-cols-4 gap-4">
          <div class="col-span-1 space-y-2">
            <h3 class="text-[12px] font-semibold text-ink-900 mb-2">源系统 (6)</h3>
            ${sourceStats}
            <div class="mt-4 p-3 rounded-lg bg-success-50/50 border border-success-500/20">
              <div class="flex items-center gap-1.5 text-[11px] font-semibold text-success-600"><i data-lucide="shield-check" class="w-3.5 h-3.5"></i>只读访问</div>
              <p class="text-[10px] text-ink-700 mt-1">所有源系统 API 只读，证据带 SHA-256 hash + 时间戳</p>
            </div>
          </div>
          <div class="col-span-3">
            <div class="flex items-center justify-between mb-3">
              <h3 class="text-[12px] font-semibold">证据卡片 (${(c.evidence || []).length})</h3>
              <div class="flex gap-1.5 text-[11px]">
                <button class="px-2 py-1 rounded bg-brand-50 text-brand-600 font-medium">全部</button>
                <button class="px-2 py-1 rounded text-ink-500 hover:bg-ink-100">高置信度</button>
                <button class="px-2 py-1 rounded text-ink-500 hover:bg-ink-100">异常</button>
              </div>
            </div>
            <div class="grid grid-cols-3 gap-3">${cards}</div>
          </div>
        </div>
      `;
    },

    renderWhysTab(c) {
      const whys = (c.fiveWhys || []).map(w => {
        const evRefs = (w.evidence || []).map(eid => `<button onclick="app.openEvidence('${eid}')" class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-brand-50 text-brand-600 hover:bg-brand-100 font-medium">${eid}</button>`).join(' ');
        const statusBadge = {
          accepted: '<span class="text-[10px] px-1.5 py-0.5 rounded bg-success-50 text-success-600 font-medium">✓ 已接受</span>',
          pending: '<span class="text-[10px] px-1.5 py-0.5 rounded bg-warning-50 text-warning-600 font-medium">⏳ 待确认</span>',
          rejected: '<span class="text-[10px] px-1.5 py-0.5 rounded bg-danger-50 text-danger-600 font-medium">✗ 已拒绝</span>',
          edited: '<span class="text-[10px] px-1.5 py-0.5 rounded bg-brand-50 text-brand-600 font-medium">✎ 已修改</span>',
        }[w.status] || '';

        return `
          <div class="bg-panel rounded-xl border border-line p-4 ${w.isRoot ? 'border-danger-500/30 bg-danger-50/20' : ''}">
            <div class="flex items-center gap-2 mb-2">
              <span class="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold ${w.isRoot ? 'bg-danger-500 text-white' : 'bg-brand-500 text-white'}">${w.n}</span>
              <span class="text-[12px] font-semibold text-ink-900 flex-1">Why ${w.n}${w.isRoot ? ' · 根因' : ''}</span>
              ${statusBadge}
              <div class="flex gap-0.5">${[1,2,3,4].map(n => `<span class="conf-dot ${n <= w.confidence ? 'bg-brand-500' : 'bg-ink-300'}"></span>`).join('')}</div>
            </div>
            <div class="text-[12px] text-ink-500 mb-1">${w.why}</div>
            <div class="text-[13px] text-ink-900 bg-ink-100/40 rounded-lg p-2.5 border border-line">${w.answer}</div>
            <div class="flex items-center gap-1.5 mt-2 flex-wrap">
              <span class="text-[10px] text-ink-500">证据:</span>
              ${evRefs || '<span class="text-[10px] text-danger-600">⚠ 无证据支持</span>'}
            </div>
            <!-- HITL buttons -->
            <div class="flex items-center gap-2 mt-3 pt-3 border-t border-line">
              <button onclick="app.acceptWhy(${w.n})" class="hitl-btn px-3 py-1.5 rounded-lg bg-success-500 hover:bg-success-600 text-white text-[11px] font-medium flex items-center gap-1 ${w.status === 'accepted' ? 'opacity-50' : ''}"><i data-lucide="check" class="w-3 h-3"></i>接受</button>
              <button onclick="app.rejectWhy(${w.n})" class="hitl-btn px-3 py-1.5 rounded-lg border border-danger-500/30 text-danger-600 hover:bg-danger-50 text-[11px] font-medium flex items-center gap-1"><i data-lucide="x" class="w-3 h-3"></i>拒绝</button>
              <button onclick="app.editWhy(${w.n})" class="hitl-btn px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-ink-700 text-[11px] font-medium flex items-center gap-1"><i data-lucide="edit-3" class="w-3 h-3"></i>修改</button>
              <div class="flex-1"></div>
              <span class="text-[10px] text-ink-300 font-mono">HITL 关卡 2 · 不可跳过</span>
            </div>
          </div>
        `;
      }).join('');

      return `
        <div class="p-5 max-w-3xl mx-auto space-y-3">
          <div class="bg-panel rounded-xl border border-line p-4 flex items-center gap-3">
            <div class="w-10 h-10 rounded-lg bg-brand-50 flex items-center justify-center"><i data-lucide="list-ordered" class="w-5 h-5 text-brand-600"></i></div>
            <div class="flex-1">
              <h3 class="text-[13px] font-semibold">5 Whys 迭代分析</h3>
              <p class="text-[11px] text-ink-500">每条结论需 ≥2 独立来源证据 (三角验证) · Reasoner v2 · 已采纳 Skeptic SK-2</p>
            </div>
            <span class="text-[10px] font-mono px-2 py-1 rounded bg-ink-100 text-ink-700">claude-sonnet-4</span>
          </div>
          ${whys}
        </div>
      `;
    },

    renderReportTab(c) {
      if (!c.report) return '<div class="p-8 text-center text-ink-500">无报告数据</div>';
      const sections = c.report.sections.map(s => {
        const evRefs = (s.evidence || []).map(eid => `<button onclick="app.openEvidence('${eid}')" class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-brand-50 text-brand-600 hover:bg-brand-100 font-medium">${eid}</button>`).join(' ');
        return `
          <div class="bg-panel rounded-xl border border-line p-4 ${s.edited ? 'border-warning-500/30' : ''}">
            <div class="flex items-center gap-2 mb-2">
              <h3 class="text-[13px] font-semibold text-ink-900 flex-1">${s.title}</h3>
              ${s.edited ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-warning-50 text-warning-600 font-medium"><i data-lucide="edit-3" class="w-2.5 h-2.5 inline"></i> 已编辑</span>' : ''}
              <button class="text-[10px] text-ink-500 hover:text-brand-600"><i data-lucide="edit-3" class="w-3 h-3"></i></button>
            </div>
            <p class="text-[12px] text-ink-700 leading-relaxed whitespace-pre-line">${s.content}</p>
            ${evRefs ? `<div class="flex items-center gap-1.5 mt-2 flex-wrap"><span class="text-[10px] text-ink-500">证据:</span>${evRefs}</div>` : ''}
          </div>
        `;
      }).join('');

      return `
        <div class="p-5 grid grid-cols-3 gap-4">
          <div class="col-span-2 space-y-3">
            <div class="bg-panel rounded-xl border border-line p-3 flex items-center gap-3">
              <div class="w-8 h-8 rounded-lg bg-success-50 flex items-center justify-center"><i data-lucide="file-text" class="w-4 h-4 text-success-600"></i></div>
              <div class="flex-1">
                <div class="text-[13px] font-semibold">ICH Q9 调查报告 · ${c.report.version}</div>
                <div class="text-[10px] text-ink-500 font-mono">最后更新: ${c.report.lastUpdated} · 模板: ICH Q9 (R1)</div>
              </div>
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[11px] text-ink-700 flex items-center gap-1"><i data-lucide="download" class="w-3 h-3"></i>导出 PDF</button>
            </div>
            ${sections}
          </div>
          <div class="space-y-3">
            <div class="bg-panel rounded-xl border border-line p-4">
              <h4 class="text-[12px] font-semibold mb-2">签字流</h4>
              <div class="space-y-2">
                <div class="flex items-center gap-2 text-[11px]"><div class="w-6 h-6 rounded-full bg-warning-50 flex items-center justify-center text-warning-600"><i data-lucide="clock" class="w-3 h-3"></i></div><div class="flex-1"><div class="font-medium">调查员提交</div><div class="text-ink-500">Lisa Chen · 待签</div></div></div>
                <div class="ml-3 w-0.5 h-3 bg-line"></div>
                <div class="flex items-center gap-2 text-[11px]"><div class="w-6 h-6 rounded-full bg-ink-100 flex items-center justify-center text-ink-500"><i data-lucide="circle" class="w-3 h-3"></i></div><div class="flex-1"><div class="font-medium text-ink-500">QA 审核</div><div class="text-ink-500">James Wilson · 等待中</div></div></div>
                <div class="ml-3 w-0.5 h-3 bg-line"></div>
                <div class="flex items-center gap-2 text-[11px]"><div class="w-6 h-6 rounded-full bg-ink-100 flex items-center justify-center text-ink-500"><i data-lucide="archive" class="w-3 h-3"></i></div><div class="flex-1"><div class="font-medium text-ink-500">归档</div><div class="text-ink-500">待 QA 批准</div></div></div>
              </div>
            </div>
            <div class="bg-panel rounded-xl border border-line p-4">
              <h4 class="text-[12px] font-semibold mb-2">版本历史</h4>
              <div class="space-y-1.5 text-[11px]">
                <div class="flex items-center gap-2"><span class="font-mono text-brand-600 font-semibold">v2</span><span class="text-ink-500">当前</span><span class="text-ink-300 ml-auto">${c.report.lastUpdated.split(' ')[0]}</span></div>
                <div class="flex items-center gap-2 text-ink-500"><span class="font-mono">v1</span><span>初稿</span><span class="text-ink-300 ml-auto">2025-08-17</span></div>
              </div>
            </div>
            <div class="bg-danger-50/40 rounded-xl border border-danger-500/20 p-3">
              <div class="flex items-center gap-1.5 text-[11px] font-semibold text-danger-600"><i data-lucide="alert-triangle" class="w-3.5 h-3.5"></i>Guardian 提示</div>
              <p class="text-[10px] text-ink-700 mt-1">PCR 报告 (E-LIMS-01) 未签收，提交前需确认。</p>
            </div>
          </div>
        </div>
      `;
    },

    // ---- P4: Evidence Library ----
    renderEvidenceLibrary() {
      // Aggregate all evidence across cases
      const allEvidence = [];
      MockData.cases.forEach(c => {
        (c.evidence || []).forEach(ev => allEvidence.push({ ...ev, caseId: c.id, caseTitle: c.title }));
      });

      const cards = allEvidence.slice(0, 18).map(ev => {
        const src = MockData.sourceSystems[ev.source];
        return `
          <div class="bg-panel rounded-lg border border-line p-3 card-hover cursor-pointer" onclick="app.openEvidence('${ev.id}')">
            <div class="flex items-center gap-2 mb-2">
              <div class="w-6 h-6 rounded-md flex items-center justify-center ${src?.color || ''}"><i data-lucide="${src?.icon || 'file'}" class="w-3 h-3"></i></div>
              <span class="text-[10px] font-mono font-semibold">${ev.id}</span>
              <span class="text-[9px] font-mono text-ink-500 ml-auto">${ev.caseId}</span>
            </div>
            <div class="text-[11px] text-ink-700 line-clamp-2 leading-snug">${ev.summary}</div>
            <div class="flex items-center gap-1 mt-2">${[1,2,3,4].map(n => `<span class="conf-dot ${n <= ev.confidence ? 'bg-brand-500' : 'bg-ink-300'}"></span>`).join('')}</div>
          </div>
        `;
      }).join('');

      return `
        <div class="p-6 max-w-[1400px] mx-auto pb-24 space-y-5">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-[20px] font-bold">证据库</h1>
              <p class="text-[12px] text-ink-500 mt-0.5">跨案例证据检索 · ${allEvidence.length} 条 · 6 源系统</p>
            </div>
            <div class="flex gap-2">
              <div class="relative">
                <i data-lucide="search" class="w-3.5 h-3.5 text-ink-500 absolute left-2.5 top-1/2 -translate-y-1/2"></i>
                <input type="text" placeholder="搜索证据关键词、ID、人员..." class="pl-8 pr-3 py-1.5 rounded-lg border border-line bg-panel text-[12px] w-64 focus-ring" />
              </div>
            </div>
          </div>
          <!-- Filters -->
          <div class="flex items-center gap-2 text-[11px]">
            <span class="text-ink-500">源系统:</span>
            ${['LIMS','MES','EMS','CMMS','LMS','BMS'].map(s => `<button class="px-2 py-1 rounded border border-line hover:bg-ink-100 ${MockData.sourceSystems[s].color} font-mono font-medium">${s}</button>`).join('')}
            <div class="flex-1"></div>
            <span class="text-ink-500">视图:</span>
            <button class="px-2 py-1 rounded bg-brand-50 text-brand-600 font-medium">网格</button>
            <button class="px-2 py-1 rounded text-ink-500 hover:bg-ink-100">图谱</button>
          </div>
          <!-- Cross-case graph -->
          <div class="bg-panel rounded-xl border border-line p-4">
            <div class="flex items-center justify-between mb-3">
              <div>
                <h3 class="text-[13px] font-semibold">跨案例证据关联图谱</h3>
                <p class="text-[11px] text-ink-500">力导向图 · EMP-2187 出现在 3 个案例中</p>
              </div>
            </div>
            <div id="evidence-graph" style="height:280px;"></div>
          </div>
          <!-- Grid -->
          <div class="grid grid-cols-6 gap-3">${cards}</div>
        </div>
      `;
    },

    renderEvidenceCharts() {
      this.$nextTick(() => {
        // Build cross-case topology from all evidence
        const nodes = [
          { id: 'EMP-2187', label: 'EMP-2187', type: 'person', source: 'person' },
          { id: 'Room-3B', label: 'Room-3B', type: 'room', source: 'room' },
          { id: 'AHU-7', label: 'AHU-7', type: 'equipment', source: 'equipment' },
          { id: 'DEV-147', label: 'DEV-147', type: 'case', source: 'case' },
          { id: 'DEV-149', label: 'DEV-149', type: 'case', source: 'case' },
          { id: 'DEV-103', label: 'DEV-103', type: 'case', source: 'case' },
          { id: 'E-LMS-01', label: '资质逾期', type: 'evidence', source: 'LMS' },
          { id: 'E-CCTV-01', label: 'CCTV异常', type: 'evidence', source: 'MES' },
          { id: 'E-BMS-05', label: '差压波动', type: 'evidence', source: 'BMS' },
          { id: 'E-CMMS-03', label: '门封变形', type: 'evidence', source: 'CMMS' },
        ];
        const links = [
          { source: 'EMP-2187', target: 'DEV-147' },
          { source: 'EMP-2187', target: 'DEV-103' },
          { source: 'EMP-2187', target: 'E-LMS-01' },
          { source: 'EMP-2187', target: 'E-CCTV-01' },
          { source: 'Room-3B', target: 'DEV-147' },
          { source: 'Room-3B', target: 'E-CCTV-01' },
          { source: 'AHU-7', target: 'DEV-147' },
          { source: 'AHU-7', target: 'DEV-149' },
          { source: 'AHU-7', target: 'E-BMS-05' },
          { source: 'E-LMS-01', target: 'DEV-147' },
          { source: 'E-CCTV-01', target: 'DEV-147' },
          { source: 'E-BMS-05', target: 'DEV-147' },
          { source: 'E-CMMS-03', target: 'DEV-147' },
        ];
        RCACharts.renderTopology('evidence-graph', { nodes, links }, (node) => {
          if (node.type === 'evidence') this.openEvidence(node.id);
          else this.toast(`节点: ${node.label} (${node.type})`, 'brand', 'share-2');
        });
      });
    },

    // ---- P5: Report Preview ----
    renderReport() {
      const c = this.activeCase;
      if (!c || !c.report) {
        return '<div class="p-8 text-center text-ink-500">请先在调查工作台生成报告</div>';
      }
      const toc = c.report.sections.map(s => `<a href="#${s.id}" class="block text-[11px] text-ink-500 hover:text-brand-600 py-1 ${s.edited ? 'text-warning-600 font-medium' : ''}">${s.title}</a>`).join('');
      const sections = c.report.sections.map(s => {
        const evRefs = (s.evidence || []).map(eid => `<button onclick="app.openEvidence('${eid}')" class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-brand-50 text-brand-600 hover:bg-brand-100 font-medium">${eid}</button>`).join(' ');
        return `
          <div id="${s.id}" class="mb-5">
            <div class="flex items-center gap-2 mb-1.5">
              <h3 class="text-[13px] font-bold text-ink-900">${s.title}</h3>
              ${s.edited ? '<span class="text-[9px] px-1.5 py-0.5 rounded bg-warning-50 text-warning-600 font-medium">已编辑</span>' : ''}
            </div>
            <p class="text-[12px] text-ink-700 leading-relaxed whitespace-pre-line">${s.content}</p>
            ${evRefs ? `<div class="flex items-center gap-1.5 mt-2 flex-wrap"><span class="text-[10px] text-ink-500 font-medium">证据引用:</span>${evRefs}</div>` : ''}
          </div>
        `;
      }).join('');

      return `
        <div class="p-6 max-w-[1200px] mx-auto pb-24">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h1 class="text-[20px] font-bold">报告预览 · ${c.report.version}</h1>
              <p class="text-[12px] text-ink-500 mt-0.5 font-mono">${c.id} · ${c.report.lastUpdated}</p>
            </div>
            <div class="flex gap-2">
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5"><i data-lucide="history" class="w-3.5 h-3.5"></i>版本 Diff</button>
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5"><i data-lucide="printer" class="w-3.5 h-3.5"></i>打印</button>
              <button class="px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-[12px] font-medium flex items-center gap-1.5" onclick="app.toast('PDF 导出中... (Demo)','brand','download')"><i data-lucide="download" class="w-3.5 h-3.5"></i>导出 PDF</button>
            </div>
          </div>
          <div class="grid grid-cols-4 gap-5">
            <!-- TOC -->
            <div class="col-span-1">
              <div class="bg-panel rounded-xl border border-line p-4 sticky top-0">
                <h4 class="text-[11px] font-semibold text-ink-500 uppercase mb-2">目录</h4>
                ${toc}
                <div class="mt-4 pt-3 border-t border-line">
                  <h4 class="text-[11px] font-semibold text-ink-500 uppercase mb-2">合规</h4>
                  <div class="space-y-1.5 text-[10px]">
                    <div class="flex items-center gap-1.5 text-success-600"><i data-lucide="check-circle-2" class="w-3 h-3"></i>ICH Q9 结构</div>
                    <div class="flex items-center gap-1.5 text-success-600"><i data-lucide="check-circle-2" class="w-3 h-3"></i>ALCOA+ 9 项</div>
                    <div class="flex items-center gap-1.5 text-warning-600"><i data-lucide="alert-circle" class="w-3 h-3"></i>1 项证据待签收</div>
                  </div>
                </div>
              </div>
            </div>
            <!-- Report body -->
            <div class="col-span-3 bg-panel rounded-xl border border-line p-6">${sections}</div>
          </div>
          <!-- Signature block -->
          <div class="mt-5 bg-panel rounded-xl border border-line p-5">
            <h3 class="text-[13px] font-semibold mb-3">电子签名 (21 CFR Part 11)</h3>
            <div class="grid grid-cols-2 gap-4">
              <div class="border border-line rounded-lg p-4">
                <div class="text-[11px] text-ink-500 mb-1">调查员</div>
                <div class="flex items-center gap-2 mb-2"><div class="w-8 h-8 rounded-full bg-brand-100 flex items-center justify-center text-brand-600 text-[11px] font-semibold">LC</div><div><div class="text-[12px] font-medium">Lisa Chen</div><div class="text-[10px] text-ink-500 font-mono">lisa.chen@biopharma.com</div></div></div>
                <button class="w-full px-3 py-2 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-[12px] font-medium" onclick="app.toast('电子签名已记录 (Part 11)','success','pen-tool'); app.addAuditLog('E_SIGNATURE','report','调查员签名提交',[])">提交签名</button>
                <p class="text-[9px] text-ink-500 mt-1.5">我确认本电子签名等同于手写签名</p>
              </div>
              <div class="border border-line rounded-lg p-4 opacity-60">
                <div class="text-[11px] text-ink-500 mb-1">QA 审核员</div>
                <div class="flex items-center gap-2 mb-2"><div class="w-8 h-8 rounded-full bg-success-100 flex items-center justify-center text-success-600 text-[11px] font-semibold">JW</div><div><div class="text-[12px] font-medium">James Wilson</div><div class="text-[10px] text-ink-500 font-mono">等待调查员提交</div></div></div>
                <button disabled class="w-full px-3 py-2 rounded-lg bg-ink-100 text-ink-500 text-[12px] font-medium cursor-not-allowed">等待中</button>
              </div>
            </div>
          </div>
        </div>
      `;
    },

    // ---- P6: Audit Trail ----
    renderAuditTrail() {
      const logs = MockData.auditLogs;
      const logRows = logs.map(l => `
        <tr class="border-b border-line/60 hover:bg-ink-100/30 text-[11px]">
          <td class="px-3 py-2 font-mono text-ink-500">${l.seq}</td>
          <td class="px-3 py-2 font-mono text-ink-700">${l.time.split('T')[1].slice(0, 8)}</td>
          <td class="px-3 py-2 text-ink-900">${l.operator === 'system' ? '<span class="text-ink-500">system</span>' : l.operator}</td>
          <td class="px-3 py-2"><span class="font-mono px-1.5 py-0.5 rounded ${l.action.includes('CHALLENGE') ? 'bg-danger-50 text-danger-600' : l.action.includes('AGENT') ? 'bg-brand-50 text-brand-600' : 'bg-ink-100 text-ink-700'}">${l.action}</span></td>
          <td class="px-3 py-2 text-ink-700">${l.target}</td>
          <td class="px-3 py-2 text-ink-500 max-w-xs truncate" title="${l.detail}">${l.detail}</td>
          <td class="px-3 py-2 font-mono text-[9px] text-ink-500">${l.hash.slice(6, 22)}</td>
        </tr>
      `).join('');

      return `
        <div class="p-6 max-w-[1400px] mx-auto pb-24 space-y-4">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-[20px] font-bold">审计追踪</h1>
              <p class="text-[12px] text-ink-500 mt-0.5">不可篡改 · 链式 hash · ${logs.length} 条记录</p>
            </div>
            <div class="flex gap-2">
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5" onclick="app.toast('JSON 导出 (Demo)','brand','download')"><i data-lucide="braces" class="w-3.5 h-3.5"></i>JSON</button>
              <button class="px-3 py-1.5 rounded-lg border border-line hover:bg-ink-100 text-[12px] text-ink-700 flex items-center gap-1.5" onclick="app.toast('CSV 导出 (Demo)','brand','download')"><i data-lucide="sheet" class="w-3.5 h-3.5"></i>CSV</button>
              <button class="px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-[12px] font-medium flex items-center gap-1.5" onclick="app.toast('审计包导出 (含验证脚本)','success','shield-check')"><i data-lucide="package" class="w-3.5 h-3.5"></i>导出审计包</button>
            </div>
          </div>
          <!-- Hash chain visualization -->
          <div class="bg-panel rounded-xl border border-line p-4">
            <div id="audit-hashchain"></div>
          </div>
          <!-- Filters -->
          <div class="flex items-center gap-2 text-[11px]">
            <select class="border border-line rounded-lg px-2 py-1.5 bg-panel text-ink-700"><option>全部操作人</option><option>Lisa Chen</option><option>System</option><option>Agent</option></select>
            <select class="border border-line rounded-lg px-2 py-1.5 bg-panel text-ink-700"><option>全部操作类型</option><option>AGENT_*</option><option>SIGNATURE</option><option>EDIT</option></select>
            <input type="text" placeholder="搜索..." class="px-3 py-1.5 rounded-lg border border-line bg-panel w-48" />
            <div class="flex-1"></div>
            <span class="text-ink-500">时间范围:</span>
            <input type="date" class="border border-line rounded-lg px-2 py-1.5 bg-panel text-ink-700" value="2025-08-17" />
            <span class="text-ink-300">—</span>
            <input type="date" class="border border-line rounded-lg px-2 py-1.5 bg-panel text-ink-700" value="2025-08-17" />
          </div>
          <!-- Table -->
          <div class="bg-panel rounded-xl border border-line overflow-hidden">
            <table class="w-full">
              <thead class="bg-ink-100/40 text-[10px] uppercase text-ink-500 font-semibold">
                <tr>
                  <th class="px-3 py-2 text-left">#</th>
                  <th class="px-3 py-2 text-left">时间</th>
                  <th class="px-3 py-2 text-left">操作人</th>
                  <th class="px-3 py-2 text-left">动作</th>
                  <th class="px-3 py-2 text-left">目标</th>
                  <th class="px-3 py-2 text-left">详情</th>
                  <th class="px-3 py-2 text-left">Hash</th>
                </tr>
              </thead>
              <tbody>${logRows}</tbody>
            </table>
          </div>
          <!-- Compliance footer -->
          <div class="grid grid-cols-3 gap-4">
            <div class="bg-success-50/40 rounded-xl border border-success-500/20 p-4">
              <div class="flex items-center gap-2"><i data-lucide="shield-check" class="w-4 h-4 text-success-600"></i><span class="text-[12px] font-semibold text-success-600">ALCOA+ 合规</span></div>
              <p class="text-[10px] text-ink-700 mt-1">9 项原则全部满足 · 每条日志含操作人/时间/IP/证据 hash</p>
            </div>
            <div class="bg-success-50/40 rounded-xl border border-success-500/20 p-4">
              <div class="flex items-center gap-2"><i data-lucide="link" class="w-4 h-4 text-success-600"></i><span class="text-[12px] font-semibold text-success-600">链式防篡改</span></div>
              <p class="text-[10px] text-ink-700 mt-1">每条日志含前序 hash · 任何修改将破坏链条 · 导出包含验证脚本</p>
            </div>
            <div class="bg-success-50/40 rounded-xl border border-success-500/20 p-4">
              <div class="flex items-center gap-2"><i data-lucide="file-lock-2" class="w-4 h-4 text-success-600"></i><span class="text-[12px] font-semibold text-success-600">21 CFR Part 11</span></div>
              <p class="text-[10px] text-ink-700 mt-1">电子签名唯一链接个人 · 保留期 10 年 · 不可覆盖删除</p>
            </div>
          </div>
        </div>
      `;
    },

    renderAuditCharts() {
      this.$nextTick(() => {
        RCACharts.renderHashChain('audit-hashchain', MockData.auditLogs);
      });
    },

    // ---- P7: Knowledge Hub ----
    renderKnowledge() {
      const k = MockData.knowledge;
      const patterns = k.rootCausePatterns.map(p => {
        const trendIcon = p.trend === 'up' ? '<i data-lucide="trending-up" class="w-3 h-3 text-danger-600"></i>' : p.trend === 'down' ? '<i data-lucide="trending-down" class="w-3 h-3 text-success-600"></i>' : '<i data-lucide="minus" class="w-3 h-3 text-ink-500"></i>';
        return `
          <div class="bg-panel rounded-lg border border-line p-3 card-hover">
            <div class="flex items-center gap-2 mb-1.5">
              <span class="text-[10px] font-mono px-1.5 py-0.5 rounded ${p.category === '人员' ? 'bg-danger-50 text-danger-600' : p.category === '设备' ? 'bg-warning-50 text-warning-600' : p.category === '方法' ? 'bg-success-50 text-success-600' : 'bg-brand-50 text-brand-600'}">${p.category}</span>
              <span class="text-[12px] font-medium text-ink-900 flex-1 truncate">${p.pattern}</span>
              ${trendIcon}
            </div>
            <div class="flex items-center gap-3 text-[10px] text-ink-500">
              <span><b class="text-ink-900 text-[12px]">${p.count}</b> 案例</span>
              <span>CAPA 效力 <b class="${p.capaEffectiveness >= 80 ? 'text-success-600' : p.capaEffectiveness >= 65 ? 'text-warning-600' : 'text-danger-600'}">${p.capaEffectiveness}%</b></span>
            </div>
            <div class="mt-1.5 h-1 bg-ink-100 rounded-full overflow-hidden"><div class="h-full ${p.capaEffectiveness >= 80 ? 'bg-success-500' : p.capaEffectiveness >= 65 ? 'bg-warning-500' : 'bg-danger-500'}" style="width:${p.capaEffectiveness}%"></div></div>
          </div>
        `;
      }).join('');

      return `
        <div class="p-6 max-w-[1400px] mx-auto pb-24 space-y-5">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-[20px] font-bold flex items-center gap-2">知识库 <span class="text-[10px] px-2 py-0.5 rounded-full bg-brand-50 text-brand-600 font-semibold">V2 预览</span></h1>
              <p class="text-[12px] text-ink-500 mt-0.5">跨案例根因模式聚类 · CAPA 效力追踪 · Memory Bank</p>
            </div>
          </div>
          <!-- Clusters -->
          <div class="bg-panel rounded-xl border border-line p-4">
            <h3 class="text-[13px] font-semibold mb-1">案例聚类 (UMAP 降维)</h3>
            <p class="text-[11px] text-ink-500 mb-3">向量嵌入 + 聚类 · ${k.clusters.reduce((s,c)=>s+c.count,0)} 历史案例</p>
            <div id="knowledge-clusters" style="height:300px;"></div>
          </div>
          <div class="grid grid-cols-3 gap-4">
            <!-- Patterns -->
            <div class="col-span-2">
              <div class="bg-panel rounded-xl border border-line p-4">
                <div class="flex items-center justify-between mb-3">
                  <h3 class="text-[13px] font-semibold">根因模式 Top 8</h3>
                  <span class="text-[10px] text-ink-500">按案例数排序</span>
                </div>
                <div class="grid grid-cols-2 gap-3">${patterns}</div>
              </div>
            </div>
            <!-- CAPA radar -->
            <div class="bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[13px] font-semibold mb-1">CAPA 效力雷达</h3>
              <p class="text-[11px] text-ink-500 mb-3">当前 vs 目标</p>
              <div style="height:260px;"><canvas id="capa-radar-chart"></canvas></div>
            </div>
          </div>
        </div>
      `;
    },

    renderKnowledgeCharts() {
      this.$nextTick(() => {
        RCACharts.renderClusters('knowledge-clusters', MockData.knowledge.clusters);
        RCACharts.renderCAPARadar('capa-radar-chart', MockData.knowledge.capaEffectiveness);
      });
    },

    // ---- P8: Admin ----
    renderAdmin() {
      const a = MockData.admin;
      const matrixRows = a.permissionMatrix.map(r => `
        <tr class="border-b border-line/60 text-[11px]">
          <td class="px-3 py-2 font-medium text-ink-900">${r.resource}</td>
          <td class="px-3 py-2 text-center ${r.admin==='RW'?'text-success-600 font-semibold':r.admin==='R'?'text-brand-600':r.admin==='—'?'text-ink-300':'text-warning-600'}">${r.admin}</td>
          <td class="px-3 py-2 text-center ${r.investigator==='RW'?'text-success-600 font-semibold':r.investigator==='R'?'text-brand-600':r.investigator==='—'?'text-ink-300':'text-warning-600'}">${r.investigator}</td>
          <td class="px-3 py-2 text-center ${r.qa_reviewer==='RW'?'text-success-600 font-semibold':r.qa_reviewer==='R'?'text-brand-600':r.qa_reviewer==='—'?'text-ink-300':'text-warning-600'}">${r.qa_reviewer}</td>
          <td class="px-3 py-2 text-center ${r.ops_manager==='RW'?'text-success-600 font-semibold':r.ops_manager==='R'?'text-brand-600':r.ops_manager==='—'?'text-ink-300':'text-warning-600'}">${r.ops_manager}</td>
          <td class="px-3 py-2 text-center ${r.auditor==='RW'?'text-success-600 font-semibold':r.auditor==='R'?'text-brand-600':r.auditor==='—'?'text-ink-300':'text-warning-600'}">${r.auditor}</td>
        </tr>
      `).join('');

      const systems = a.systemIntegrations.map(s => {
        const sys = MockData.sourceSystems[s.id];
        const statusCls = s.status === 'healthy' ? 'text-success-600' : 'text-warning-600';
        const statusBg = s.status === 'healthy' ? 'bg-success-500' : 'bg-warning-500';
        return `
          <div class="bg-panel rounded-lg border border-line p-3">
            <div class="flex items-center gap-2 mb-2">
              <div class="w-8 h-8 rounded-md flex items-center justify-center ${sys.color}"><i data-lucide="${sys.icon}" class="w-4 h-4"></i></div>
              <div class="flex-1">
                <div class="text-[12px] font-semibold">${s.id}</div>
                <div class="text-[9px] text-ink-500 font-mono">connector ${s.connector}</div>
              </div>
              <span class="flex items-center gap-1 text-[10px] ${statusCls}"><span class="w-1.5 h-1.5 rounded-full ${statusBg} ${s.status !== 'healthy' ? 'animate-pulse-dot' : ''}"></span>${s.status}</span>
            </div>
            <div class="grid grid-cols-3 gap-2 text-[10px]">
              <div><div class="text-ink-500">延迟</div><div class="font-mono font-semibold ${s.latency > 500 ? 'text-warning-600' : 'text-ink-900'}">${s.latency}ms</div></div>
              <div><div class="text-ink-500">记录数</div><div class="font-mono font-semibold">${s.records.toLocaleString()}</div></div>
              <div><div class="text-ink-500">同步</div><div class="font-mono text-ink-700">${s.lastSync}</div></div>
            </div>
            <div class="mt-2 flex items-center gap-1.5 text-[9px] text-success-600"><i data-lucide="lock" class="w-2.5 h-2.5"></i>只读 API · 数据不写入源系统</div>
          </div>
        `;
      }).join('');

      const agentRows = a.agentModels.map(am => `
        <div class="flex items-center gap-3 py-2 border-b border-line/60 text-[11px]">
          <span class="font-medium text-ink-900 w-32">${am.agent}</span>
          <span class="font-mono text-brand-600 flex-1">${am.model}</span>
          <span class="text-ink-500">${am.role}</span>
          <button class="text-brand-600 hover:text-brand-700 font-medium">更换</button>
        </div>
      `).join('');

      return `
        <div class="p-6 max-w-[1400px] mx-auto pb-24 space-y-5">
          <h1 class="text-[20px] font-bold">管理与合规</h1>
          <!-- System integrations -->
          <div>
            <h3 class="text-[13px] font-semibold mb-2 flex items-center gap-2"><i data-lucide="plug" class="w-4 h-4 text-brand-600"></i>系统集成 (6 源系统 · 全部只读)</h3>
            <div class="grid grid-cols-3 gap-3">${systems}</div>
          </div>
          <div class="grid grid-cols-3 gap-5">
            <!-- RBAC -->
            <div class="col-span-2 bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[13px] font-semibold mb-3 flex items-center gap-2"><i data-lucide="users" class="w-4 h-4 text-brand-600"></i>角色权限矩阵 (RBAC)</h3>
              <table class="w-full">
                <thead class="text-[10px] uppercase text-ink-500 font-semibold">
                  <tr class="border-b border-line">
                    <th class="px-3 py-2 text-left">资源</th>
                    <th class="px-3 py-2 text-center">管理员</th>
                    <th class="px-3 py-2 text-center">调查员</th>
                    <th class="px-3 py-2 text-center">QA</th>
                    <th class="px-3 py-2 text-center">运营</th>
                    <th class="px-3 py-2 text-center">审计</th>
                  </tr>
                </thead>
                <tbody>${matrixRows}</tbody>
              </table>
              <div class="flex gap-3 mt-2 text-[9px] text-ink-500"><span><b class="text-success-600">RW</b> 读写</span><span><b class="text-brand-600">R</b> 只读</span><span><b class="text-warning-600">提交/批准</b></span><span class="text-ink-300">— 无权限</span></div>
            </div>
            <!-- Signature policy -->
            <div class="bg-panel rounded-xl border border-line p-4">
              <h3 class="text-[13px] font-semibold mb-3 flex items-center gap-2"><i data-lucide="pen-tool" class="w-4 h-4 text-brand-600"></i>签名策略</h3>
              <div class="space-y-2 text-[11px]">
                <div class="flex justify-between"><span class="text-ink-500">提交门槛</span><span class="font-medium">${a.signaturePolicy.submitThreshold}</span></div>
                <div class="flex justify-between"><span class="text-ink-500">批准门槛</span><span class="font-medium">${a.signaturePolicy.approveThreshold}</span></div>
                <div class="flex justify-between"><span class="text-ink-500">双人签名</span><span class="font-medium text-success-600">${a.signaturePolicy.dualSignature ? '已启用' : '未启用'}</span></div>
                <div class="flex justify-between"><span class="text-ink-500">保留期</span><span class="font-medium">${a.signaturePolicy.retentionYears} 年</span></div>
              </div>
              <div class="mt-3 p-2.5 rounded bg-ink-100/40 border border-line text-[10px] text-ink-700 italic">"${a.signaturePolicy.meaningOfSignature}"</div>
            </div>
          </div>
          <!-- Agent models -->
          <div class="bg-panel rounded-xl border border-line p-4">
            <h3 class="text-[13px] font-semibold mb-2 flex items-center gap-2"><i data-lucide="cpu" class="w-4 h-4 text-brand-600"></i>Agent 模型配置 (每个 Agent 可独立选模型)</h3>
            ${agentRows}
          </div>
        </div>
      `;
    },
  };
}

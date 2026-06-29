/* ============================================
   RCA Pilot — Mock Data
   4 Cases (Case A primary), 6 source systems,
   audit logs with hash chain, users, KPIs
   ============================================ */

window.MockData = (function () {

  // ---- Hash helper (deterministic fake SHA-256 for demo) ----
  function fakeHash(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    const base = (h >>> 0).toString(16).padStart(8, '0');
    return 'sha256:' + (base + base + base + base).slice(0, 40);
  }

  // ---- Users ----
  const users = [
    { id: 'U-LC', name: 'Lisa Chen', email: 'lisa.chen@biopharma.com', role: 'investigator', roleLabel: '调查员', dept: '微生物', avatar: 'LC', color: 'brand', shift: '晚班', cert: '微生物学博士, 5年经验' },
    { id: 'U-JW', name: 'James Wilson', email: 'james.wilson@biopharma.com', role: 'qa_reviewer', roleLabel: 'QA 审核员', dept: '质量保证', avatar: 'JW', color: 'success', shift: '—', cert: 'ASQ CQE, 12年QA经验' },
    { id: 'U-RK', name: 'Robert Kim', email: 'robert.kim@biopharma.com', role: 'ops_manager', roleLabel: '运营经理', dept: '生产运营', avatar: 'RK', color: 'warning', shift: '—', cert: '工程管理硕士, 15年产线' },
    { id: 'U-SM', name: 'Sarah Martinez', email: 'sarah.martinez@biopharma.com', role: 'investigator', roleLabel: '调查员', dept: '化学分析', avatar: 'SM', color: 'brand', shift: '白班', cert: '分析化学硕士' },
    { id: 'U-DL', name: 'David Liu', email: 'david.liu@biopharma.com', role: 'qa_reviewer', roleLabel: 'QA 审核员', dept: '质量保证', avatar: 'DL', color: 'success', shift: '—', cert: '药学博士' },
    { id: 'U-EMP-2156', name: 'EMP-2156', email: 'op2156@biopharma.com', role: 'operator', roleLabel: '清洁操作员', dept: '生产', avatar: '21', color: 'ink', shift: '晚班', cert: '更衣资质有效至 2025-12-15' },
    { id: 'U-EMP-2187', name: 'EMP-2187', email: 'op2187@biopharma.com', role: 'operator', roleLabel: '清洁操作员', dept: '生产', avatar: '21', color: 'danger', shift: '晚班', cert: '更衣资质已逾期 (2025-08-10)' },
    { id: 'U-EMP-2191', name: 'EMP-2191', email: 'op2191@biopharma.com', role: 'operator', roleLabel: '清洁操作员', dept: '生产', avatar: '21', color: 'ink', shift: '白班', cert: '更衣资质有效至 2025-11-01' },
  ];

  // ---- Source system registry ----
  const sourceSystems = {
    LIMS:  { label: 'LIMS — 实验室信息管理',  icon: 'flask-conical',  color: 'src-lims',  desc: '检验数据、菌种鉴定、批次结果' },
    MES:   { label: 'MES — 制造执行系统',     icon: 'clipboard-list', color: 'src-mes',   desc: '批记录、工艺参数、电子签名' },
    EMS:   { label: 'EMS — 环境监控系统',     icon: 'thermometer',    color: 'src-ems',   desc: '沉降菌、尘埃粒子、温湿度' },
    CMMS:  { label: 'CMMS — 设备维护管理',   icon: 'wrench',         color: 'src-cmms',  desc: '工单、预防维护、紧急维修' },
    LMS:   { label: 'LMS — 培训管理系统',     icon: 'graduation-cap', color: 'src-lms',   desc: '资质、培训记录、更衣认证' },
    BMS:   { label: 'BMS — 楼宇管理系统',     icon: 'wind',           color: 'src-bms',   desc: 'HVAC、差压、温湿度、风量' },
  };

  // ============================================================
  //  CASE A — B 级背景微生物污染 (PRIMARY)
  // ============================================================
  const caseA = {
    id: 'DEV-2025-08-147',
    title: 'B 级背景细胞培养区连续 5 天检出 Micrococcus luteus 超标',
    type: 'EM + HVAC + 培训',
    complexity: 3,
    severity: 'major',
    status: 'investigating',
    statusLabel: '调查中',
    discoveredAt: '2025-08-17 06:00',
    location: 'Suite 3, Room-3B-Act-12',
    area: 'B 级背景 · 细胞培养区',
    shift: '晚班 (18:00–06:00)',
    deadline: '2025-08-31',
    investigator: 'U-LC',
    qaReviewer: 'U-JW',
    productImpact: '相关批次 AB-2025-218 ~ AB-2025-223 待评估',
    description: '连续 5 天 (8/12–8/16) Room-3B-Act-12 沉降菌计数呈上升趋势，8/16 突破行动线 (15 CFU/孔)，达 22 CFU/孔。PCR 鉴定为 Micrococcus luteus，提示人员来源。',
    organism: 'Micrococcus luteus',
    organismSource: '人体皮肤来源菌',
    immediateAction: '1) 受影响批次 (AB-218~223) 暂停放行;\n2) Room-3B-Act-12 增加采样频率至 4h/次;\n3) EMP-2187 暂停 B 级区作业;\n4) AHU-7 预过滤器紧急更换。',
    progress: 68,
    hitlGates: { gate1: 'done', gate2: 'done', gate3: 'pending', gate4: 'pending', gate5: 'pending' },
    meta: { affectedBatches: 6, roomsInvolved: 1, personnelInvolved: 2 },

    // ---- Evidence timeline ----
    timeline: [
      { id: 'T1', time: '2025-08-10 06:00', source: 'BMS',  title: 'AHU-7 基线差压', detail: 'ΔP 380 Pa — 正常基值', severity: 'info', evidenceId: 'E-BMS-01' },
      { id: 'T2', time: '2025-08-11 17:00', source: 'MES',  title: 'Room-3B 清洁', detail: 'EMP-2156 执行，Spor-Klenz RTU', severity: 'info', evidenceId: 'E-MES-01' },
      { id: 'T3', time: '2025-08-12 18:30', source: 'BMS',  title: 'AHU-7 差压轻微上升', detail: 'ΔP 420 Pa (↑40)', severity: 'warning', evidenceId: 'E-BMS-02' },
      { id: 'T4', time: '2025-08-12 20:00', source: 'EMS',  title: 'Room-3B-Act-12 首次检出', detail: '沉降菌 3 CFU/孔 — 正常', severity: 'info', evidenceId: 'E-EMS-01' },
      { id: 'T5', time: '2025-08-12 17:22', source: 'MES',  title: 'EMP-2187 更衣异常', detail: 'CCTV: 第一层手套未完全覆盖袖口', severity: 'warning', evidenceId: 'E-CCTV-01' },
      { id: 'T6', time: '2025-08-13 21:00', source: 'EMS',  title: '沉降菌超过警戒线', detail: '8 CFU/孔 (警戒线 5)', severity: 'warning', evidenceId: 'E-EMS-02' },
      { id: 'T7', time: '2025-08-14 08:00', source: 'BMS',  title: 'AHU-7 接近维护阈值', detail: 'ΔP 470 Pa (阈值 500)', severity: 'warning', evidenceId: 'E-BMS-03' },
      { id: 'T8', time: '2025-08-14 19:30', source: 'EMS',  title: '沉降菌接近行动线', detail: '12 CFU/孔 (行动线 15)', severity: 'warning', evidenceId: 'E-EMS-03' },
      { id: 'T9', time: '2025-08-14 21:00', source: 'CMMS', title: 'AHU-7 预过滤器更换', detail: 'WO-20250814-0401 完成，ΔP 回落至 400 Pa', severity: 'info', evidenceId: 'E-CMMS-02' },
      { id: 'T10', time: '2025-08-15 21:00', source: 'EMS', title: '沉降菌达到行动线', detail: '15 CFU/孔 — 触发预警', severity: 'danger', evidenceId: 'E-EMS-04' },
      { id: 'T11', time: '2025-08-16 22:00', source: 'EMS', title: '沉降菌超过行动线 ⚠', detail: '22 CFU/孔 — 触发偏差', severity: 'danger', evidenceId: 'E-EMS-05', anomaly: true },
      { id: 'T12', time: '2025-08-16 23:00', source: 'BMS', title: 'AHU-7 差压再次上升', detail: 'ΔP 435 Pa', severity: 'warning', evidenceId: 'E-BMS-04' },
      { id: 'T13', time: '2025-08-17 04:30', source: 'LIMS', title: 'PCR 菌种鉴定', detail: 'Micrococcus luteus — 人体来源', severity: 'danger', evidenceId: 'E-LIMS-01' },
      { id: 'T14', time: '2025-08-17 06:00', source: '—',  title: '偏差正式立案', detail: 'DEV-2025-08-147 创建', severity: 'danger', evidenceId: null },
    ],

    // ---- Evidence cards (full set) ----
    evidence: [
      { id: 'E-EMS-01', source: 'EMS', timestamp: '2025-08-12T20:00:00+08:00', confidence: 4, summary: 'Room-3B-Act-12 沉降菌 3 CFU/孔 — 正常基线', hash: fakeHash('E-EMS-01'), raw: { date: '2025-08-12', time: '20:00', point: 'Room-3B-Act-12', cfu: 3, alert: 5, action: 15, status: 'normal' } },
      { id: 'E-EMS-02', source: 'EMS', timestamp: '2025-08-13T21:00:00+08:00', confidence: 4, summary: 'Room-3B-Act-12 沉降菌 8 CFU/孔 — 超过警戒线', hash: fakeHash('E-EMS-02'), raw: { date: '2025-08-13', time: '21:00', point: 'Room-3B-Act-12', cfu: 8, alert: 5, action: 15, status: 'alert' } },
      { id: 'E-EMS-03', source: 'EMS', timestamp: '2025-08-14T19:30:00+08:00', confidence: 4, summary: 'Room-3B-Act-12 沉降菌 12 CFU/孔 — 接近行动线', hash: fakeHash('E-EMS-03'), raw: { date: '2025-08-14', time: '19:30', point: 'Room-3B-Act-12', cfu: 12, alert: 5, action: 15, status: 'alert' } },
      { id: 'E-EMS-04', source: 'EMS', timestamp: '2025-08-15T21:00:00+08:00', confidence: 4, summary: 'Room-3B-Act-12 沉降菌 15 CFU/孔 — 达到行动线', hash: fakeHash('E-EMS-04'), raw: { date: '2025-08-15', time: '21:00', point: 'Room-3B-Act-12', cfu: 15, alert: 5, action: 15, status: 'action' } },
      { id: 'E-EMS-05', source: 'EMS', timestamp: '2025-08-16T22:00:00+08:00', confidence: 4, summary: 'Room-3B-Act-12 沉降菌 22 CFU/孔 — 超过行动线，触发偏差', hash: fakeHash('E-EMS-05'), raw: { date: '2025-08-16', time: '22:00', point: 'Room-3B-Act-12', cfu: 22, alert: 5, action: 15, status: 'action_exceeded' } },
      { id: 'E-LIMS-01', source: 'LIMS', timestamp: '2025-08-17T04:30:00+08:00', confidence: 4, summary: 'PCR 鉴定结果: Micrococcus luteus — 人体皮肤来源菌', hash: fakeHash('E-LIMS-01'), raw: { sample: 'S-2025-08-16-022', method: 'PCR (16S rRNA)', organism: 'Micrococcus luteus', identity: '99.2%', source_hint: 'human_skin', lab: 'Micro Lab', analyst: 'U-LC' } },
      { id: 'E-CCTV-01', source: 'MES', timestamp: '2025-08-12T17:22:00+08:00', confidence: 3, summary: 'CCTV: EMP-2187 第一层手套未完全覆盖袖口', hash: fakeHash('E-CCTV-01'), raw: { date: '2025-08-12', time: '17:22', camera: 'CAM-3B-GOWN-02', person: 'EMP-2187', action: 'gowning', anomaly: 'inner_glove_not_covered', flagged: true } },
      { id: 'E-CCTV-02', source: 'MES', timestamp: '2025-08-13T17:18:00+08:00', confidence: 3, summary: 'CCTV: EMP-2187 同类更衣异常 (第 2 次)', hash: fakeHash('E-CCTV-02'), raw: { date: '2025-08-13', time: '17:18', camera: 'CAM-3B-GOWN-02', person: 'EMP-2187', anomaly: 'inner_glove_not_covered', flagged: true } },
      { id: 'E-CCTV-03', source: 'MES', timestamp: '2025-08-14T17:25:00+08:00', confidence: 3, summary: 'CCTV: EMP-2187 同类更衣异常 (第 3 次)', hash: fakeHash('E-CCTV-03'), raw: { date: '2025-08-14', time: '17:25', camera: 'CAM-3B-GOWN-02', person: 'EMP-2187', anomaly: 'inner_glove_not_covered', flagged: true } },
      { id: 'E-CCTV-04', source: 'MES', timestamp: '2025-08-15T17:20:00+08:00', confidence: 3, summary: 'CCTV: EMP-2187 更衣异常 + 外层口罩未贴合', hash: fakeHash('E-CCTV-04'), raw: { date: '2025-08-15', time: '17:20', camera: 'CAM-3B-GOWN-02', person: 'EMP-2187', anomaly: ['inner_glove_not_covered','outer_mask_not_sealed'], flagged: true } },
      { id: 'E-LMS-01', source: 'LMS', timestamp: '2025-08-12T00:00:00+08:00', confidence: 4, summary: 'EMP-2187 更衣资质已逾期 (有效期 2025-08-10)', hash: fakeHash('E-LMS-01'), raw: { employee: 'EMP-2187', role: '清洁操作员', cert: 'B级区更衣资质', valid_until: '2025-08-10', status: 'expired', days_expired: 2, alert_triggered: false } },
      { id: 'E-LMS-02', source: 'LMS', timestamp: '2025-08-12T00:00:00+08:00', confidence: 4, summary: 'EMP-2156 更衣资质有效 (有效期 2025-12-15)', hash: fakeHash('E-LMS-02'), raw: { employee: 'EMP-2156', role: '清洁操作员', cert: 'B级区更衣资质', valid_until: '2025-12-15', status: 'valid' } },
      { id: 'E-LMS-03', source: 'LMS', timestamp: '2025-08-12T00:00:00+08:00', confidence: 3, summary: 'LMS 告警日志为空 — 资质逾期未触发系统告警', hash: fakeHash('E-LMS-03'), raw: { employee: 'EMP-2187', alert_type: 'cert_expiry', alert_log: [], expected_alert_date: '2025-08-03', gap: 'LMS与排班系统无联动' } },
      { id: 'E-BMS-01', source: 'BMS', timestamp: '2025-08-10T06:00:00+08:00', confidence: 4, summary: 'AHU-7 送风滤芯差压基线 380 Pa', hash: fakeHash('E-BMS-01'), raw: { equipment: 'AHU-7', component: 'pre_filter', metric: 'delta_P', value: 380, unit: 'Pa', status: 'normal' } },
      { id: 'E-BMS-02', source: 'BMS', timestamp: '2025-08-12T18:30:00+08:00', confidence: 4, summary: 'AHU-7 差压上升至 420 Pa (↑40)', hash: fakeHash('E-BMS-02'), raw: { equipment: 'AHU-7', metric: 'delta_P', value: 420, unit: 'Pa', delta: 40, status: 'warning' } },
      { id: 'E-BMS-03', source: 'BMS', timestamp: '2025-08-14T08:00:00+08:00', confidence: 4, summary: 'AHU-7 差压 470 Pa — 接近维护阈值 500', hash: fakeHash('E-BMS-03'), raw: { equipment: 'AHU-7', metric: 'delta_P', value: 470, unit: 'Pa', threshold: 500, status: 'approaching_threshold' } },
      { id: 'E-BMS-04', source: 'BMS', timestamp: '2025-08-14T21:00:00+08:00', confidence: 4, summary: 'AHU-7 更换预过滤器后差压回落至 400 Pa', hash: fakeHash('E-BMS-04'), raw: { equipment: 'AHU-7', metric: 'delta_P', value: 400, unit: 'Pa', post_maintenance: true, status: 'normal' } },
      { id: 'E-BMS-05', source: 'BMS', timestamp: '2025-08-16T23:00:00+08:00', confidence: 4, summary: 'AHU-7 差压再次上升至 435 Pa', hash: fakeHash('E-BMS-05'), raw: { equipment: 'AHU-7', metric: 'delta_P', value: 435, unit: 'Pa', post_maintenance: true, status: 'warning' } },
      { id: 'E-CMMS-01', source: 'CMMS', timestamp: '2025-08-11T10:00:00+08:00', confidence: 4, summary: 'WO-20250811-0342: AHU-7 预防维护 — 更换初效过滤器', hash: fakeHash('E-CMMS-01'), raw: { work_order: 'WO-20250811-0342', date: '2025-08-11', equipment: 'AHU-7', type: 'preventive', detail: '更换初效过滤器 G4', status: 'completed', technician: 'ENG-0042' } },
      { id: 'E-CMMS-02', source: 'CMMS', timestamp: '2025-08-14T21:00:00+08:00', confidence: 4, summary: 'WO-20250814-0401: AHU-7 紧急维修 — 更换预过滤器', hash: fakeHash('E-CMMS-02'), raw: { work_order: 'WO-20250814-0401', date: '2025-08-14', equipment: 'AHU-7', type: 'corrective', detail: '差压偏高，更换预过滤器 F8', status: 'completed', completed_at: '2025-08-14 21:00' } },
      { id: 'E-CMMS-03', source: 'CMMS', timestamp: '2025-08-16T14:00:00+08:00', confidence: 3, summary: 'WO-20250816-0457: Room-3B 门封变形 — 待维修', hash: fakeHash('E-CMMS-03'), raw: { work_order: 'WO-20250816-0457', date: '2025-08-16', equipment: 'Room-3B 门封', type: 'corrective', detail: '门封条轻微变形，可能影响压力梯度', status: 'open', priority: 'medium' } },
      { id: 'E-MES-01', source: 'MES', timestamp: '2025-08-11T17:00:00+08:00', confidence: 4, summary: 'Room-3B 清洁记录: EMP-2156, Spor-Klenz RTU', hash: fakeHash('E-MES-01'), raw: { date: '2025-08-11', room: 'Room-3B', operator: 'EMP-2156', agent: 'Spor-Klenz RTU', lot: 'SK-2507-08', status: 'completed', signed: true } },
      { id: 'E-MES-02', source: 'MES', timestamp: '2025-08-12T17:30:00+08:00', confidence: 4, summary: 'Room-3B 清洁记录: EMP-2187, Spor-Klenz RTU', hash: fakeHash('E-MES-02'), raw: { date: '2025-08-12', room: 'Room-3B', operator: 'EMP-2187', agent: 'Spor-Klenz RTU', lot: 'SK-2507-08', status: 'completed', signed: true } },
      { id: 'E-SYS-01', source: 'LMS', timestamp: '2025-08-17T08:00:00+08:00', confidence: 3, summary: '系统架构文档: LMS 与排班系统 (SAP-HR) 无资质门控集成', hash: fakeHash('E-SYS-01'), raw: { doc: 'ARCH-IT-2024-003', finding: 'LMS cert expiry does not propagate to shift scheduling system; no automated gating', integration_gap: true, recommendation: '建立 LMS↔排班系统 资质门控 API' } },
    ],

    // ---- Fishbone (6M) ----
    fishbone: {
      root: 'Room-3B-Act-12 沉降菌 M. luteus 超标',
      categories: [
        { name: '人员 Man', icon: 'users', color: 'src-lms', causes: [
          { text: 'EMP-2187 更衣资质逾期', evidence: ['E-LMS-01'], severity: 'danger' },
          { text: '更衣合规性下降 (CCTV 佐证 ×4)', evidence: ['E-CCTV-01','E-CCTV-02','E-CCTV-03','E-CCTV-04'], severity: 'danger' },
          { text: '晚班监督不足', evidence: [], severity: 'warning', note: '无证据 — 需补充排班监督记录' },
        ]},
        { name: '设备 Machine', icon: 'cpu', color: 'src-cmms', causes: [
          { text: 'AHU-7 滤芯差压异常波动', evidence: ['E-BMS-02','E-BMS-03','E-BMS-05'], severity: 'warning' },
          { text: 'Room-3B 门封变形 (压力失衡)', evidence: ['E-CMMS-03'], severity: 'warning' },
        ]},
        { name: '方法 Method', icon: 'list-checks', color: 'src-mes', causes: [
          { text: '清洁程序 SOP-3B-042 无差异', evidence: ['E-MES-01','E-MES-02'], severity: 'info', note: '已排除' },
          { text: '清洁剂浓度无异常', evidence: [], severity: 'info' },
        ]},
        { name: '材料 Material', icon: 'package', color: 'src-lims', causes: [
          { text: '清洁剂批次 SK-2507-08 无问题', evidence: ['E-MES-01'], severity: 'info', note: '已排除' },
        ]},
        { name: '测量 Measurement', icon: 'ruler', color: 'src-ems', causes: [
          { text: 'EMS 采样方法无变更', evidence: [], severity: 'info', note: '已排除' },
        ]},
        { name: '环境 Mother Nature', icon: 'cloud-rain', color: 'src-bms', causes: [
          { text: 'HVAC 差压波动 → 气流组织不均', evidence: ['E-BMS-02','E-BMS-05'], severity: 'warning' },
          { text: '晚班人员流动增加', evidence: [], severity: 'warning', note: '需补充门禁数据' },
        ]},
      ],
    },

    // ---- 5 Whys ----
    fiveWhys: [
      { n: 1, why: '为什么 Room-3B-Act-12 检出 M. luteus 超标？', answer: '人体来源菌通过更衣环节进入 B 级区', evidence: ['E-LIMS-01','E-EMS-05'], confidence: 4, status: 'accepted' },
      { n: 2, why: '为什么人体来源菌在晚班聚集？', answer: '清洁人员 EMP-2187 更衣合规性下降 (CCTV 佐证 4 次)', evidence: ['E-CCTV-01','E-CCTV-02','E-CCTV-03','E-CCTV-04'], confidence: 4, status: 'accepted' },
      { n: 3, why: '为什么 EMP-2187 更衣不合规？', answer: '更衣资质已逾期 (2025-08-10)，缺乏有效再培训', evidence: ['E-LMS-01'], confidence: 3, status: 'accepted' },
      { n: 4, why: '为什么培训逾期未触发系统告警？', answer: 'LMS 与排班系统无联动，逾期资质员工仍被排入 B 级区', evidence: ['E-LMS-03','E-SYS-01'], confidence: 3, status: 'pending' },
      { n: 5, why: '为什么 LMS 与排班系统缺乏资质门控联动？(根因)', answer: 'IT 系统架构设计时未将资质门控作为硬性集成项，仅依赖人工核查', evidence: ['E-SYS-01'], confidence: 3, status: 'pending', isRoot: true },
    ],

    // ---- Skeptic challenges ----
    skepticChallenges: [
      {
        id: 'SK-1',
        target: 'Why 2: 清洁人员 EMP-2187 更衣合规性下降是菌落聚集主因',
        challenge: '8/8 (资质逾期前 2 天) 也有同类菌落检出，但当天 EMP-2187 更衣正常。若更衣是唯一原因，为何资质有效期内也出现？HVAC 差压上升是否为并行因素？',
        evidence: [
          { id: 'E-BMS-02', summary: '8/12 差压已上升 40 Pa，早于资质逾期影响' },
          { id: 'E-BMS-05', summary: '8/16 更换过滤器后差压再次上升，说明 HVAC 有独立问题' },
        ],
        severity: 'high',
      },
      {
        id: 'SK-2',
        target: 'Why 5: LMS-排班系统无联动是根因',
        challenge: 'Room-3B-Act-08 和 Act-15 同期均在警戒线以下 (≤3 CFU)，但这两个房间也由 EMP-2187 清洁。若根因是人员资质，为何其他房间未超标？是否说明 HVAC 门封变形 (E-CMMS-03) 才是 Act-12 特异性的根因？',
        evidence: [
          { id: 'E-CMMS-03', summary: 'Room-3B 门封变形 — 仅 Act-12 受影响' },
          { id: 'E-BMS-05', summary: '差压波动导致气流组织异常，可能局部携带' },
        ],
        severity: 'critical',
      },
    ],

    // ---- Report draft sections (ICH Q9) ----
    report: {
      version: 'v2',
      lastUpdated: '2025-08-17 14:30',
      sections: [
        { id: 's1', title: '1. 偏差概述', content: '2025年8月12日至8月16日，Suite 3 B级背景细胞培养区 Room-3B-Act-12 连续5天检出沉降菌计数呈上升趋势。8月16日22:00采样结果为 22 CFU/孔，超过行动线 (15 CFU/孔)。PCR鉴定结果为 Micrococcus luteus，提示人体皮肤来源菌。本偏差定为 Major (重大)，影响批次 AB-2025-218 至 AB-2025-223，调查截止日 2025-08-31。', evidence: ['E-EMS-05','E-LIMS-01'], edited: true },
        { id: 's2', title: '2. 即时措施', content: '(1) 受影响批次 AB-218~223 暂停放行，待评估;(2) Room-3B-Act-12 采样频率提升至 4小时/次;(3) EMP-2187 暂停 B 级区作业，安排更衣再培训;(4) AHU-7 预过滤器紧急更换 (8/14 21:00 完成);(5) Room-3B 全面消毒。', evidence: ['E-CMMS-02'], edited: false },
        { id: 's3', title: '3. 调查方法', content: '采用 ICH Q9 质量风险管理框架，运用鱼骨图 (6M 分类) 与 5 Whys 迭代分析。Evidence Hunter 跨 6 系统 (LIMS/MES/EMS/CMMS/LMS/BMS) 拉取 8/10–8/17 相关数据共 23 条。Skeptic 反例 Agent 对每条结论进行挑战，确保结论经三角验证。', evidence: [], edited: false },
        { id: 's4', title: '4. 根本原因分析', content: '通过 5 Whys 迭代分析，根因为: LMS 培训管理系统与排班系统 (SAP-HR) 缺乏资质门控联动，导致更衣资质逾期的员工 (EMP-2187) 仍被排入 B 级区清洁作业，更衣合规性下降 (CCTV 佐证 4 次)，叠加 HVAC 差压波动 (AHU-7 滤芯问题 + 门封变形)，人体来源菌经气流组织异常区域进入 Act-12。', evidence: ['E-LMS-01','E-LMS-03','E-SYS-01','E-CCTV-01','E-CCTV-04','E-BMS-05','E-CMMS-03'], edited: true },
        { id: 's5', title: '5. 影响评估', content: '产品影响: 批次 AB-218~223 (6批) 需进行微生物风险评估，基于 Act-12 与 Act-08/15 的菌落数差异 (22 vs ≤3 CFU)，初步判断污染局限于 Act-12 区域。需评估这些批次是否经 Act-12 暴露。质量风险: 中等 — 菌种为人体常见菌，非致病，但持续超标表明屏障失效。', evidence: ['E-EMS-05'], edited: false },
        { id: 's6', title: '6. CAPA 建议', content: '纠正措施 (CA): (1) EMP-2187 立即更衣再培训并考核;(2) Room-3B 门封更换 (WO-0457 优先级提升);(3) AHU-7 增加差压监控频率。预防措施 (PA): (1) 建立 LMS↔排班系统资质门控 API，逾期自动拦截排班;(2) CCTV 更衣 AI 识别系统部署;(3) 全厂 LMS 资质审计。', evidence: ['E-SYS-01','E-CMMS-03'], edited: true },
        { id: 's7', title: '7. 结论与签字', content: '本偏差根因为系统级资质门控缺失，叠加设备维护滞后。CAPA 闭环后预计 CAPA 复发率降低 40%。调查员: Lisa Chen (待签); QA Reviewer: James Wilson (待审)。', evidence: [], edited: false },
      ],
    },

    // ---- Evidence topology (for D3 graph) ----
    topology: {
      nodes: [
        { id: 'E-EMS-05', label: 'EM超标', type: 'evidence', source: 'EMS' },
        { id: 'E-LIMS-01', label: 'PCR鉴定', type: 'evidence', source: 'LIMS' },
        { id: 'E-CCTV-01', label: 'CCTV×4', type: 'evidence', source: 'MES' },
        { id: 'E-LMS-01', label: '资质逾期', type: 'evidence', source: 'LMS' },
        { id: 'E-LMS-03', label: '告警空', type: 'evidence', source: 'LMS' },
        { id: 'E-SYS-01', label: '架构缺陷', type: 'evidence', source: 'LMS' },
        { id: 'E-BMS-05', label: '差压波动', type: 'evidence', source: 'BMS' },
        { id: 'E-CMMS-03', label: '门封变形', type: 'evidence', source: 'CMMS' },
        { id: 'C1', label: '人体来源菌', type: 'conclusion' },
        { id: 'C2', label: '更衣合规↓', type: 'conclusion' },
        { id: 'C3', label: '资质逾期', type: 'conclusion' },
        { id: 'C4', label: '系统无联动', type: 'conclusion' },
        { id: 'ROOT', label: '根因', type: 'root' },
      ],
      links: [
        { source: 'E-EMS-05', target: 'C1' },
        { source: 'E-LIMS-01', target: 'C1' },
        { source: 'E-CCTV-01', target: 'C2' },
        { source: 'C1', target: 'C2' },
        { source: 'C2', target: 'C3' },
        { source: 'E-LMS-01', target: 'C3' },
        { source: 'C3', target: 'C4' },
        { source: 'E-LMS-03', target: 'C4' },
        { source: 'E-SYS-01', target: 'C4' },
        { source: 'C4', target: 'ROOT' },
        { source: 'E-BMS-05', target: 'C1' },
        { source: 'E-CMMS-03', target: 'C1' },
      ],
    },

    // ---- EM trend data for chart ----
    emTrend: {
      labels: ['8/12', '8/13', '8/14', '8/15', '8/16'],
      act12: [3, 8, 12, 15, 22],
      act08: [1, 2, 2, 3, 2],
      act15: [1, 1, 2, 2, 3],
      alertLine: 5,
      actionLine: 15,
    },

    // ---- HVAC pressure data ----
    hvacTrend: {
      labels: ['8/10 06:00', '8/12 18:30', '8/13 12:00', '8/14 08:00', '8/14 21:00', '8/16 23:00'],
      values: [380, 420, 445, 470, 400, 435],
      threshold: 500,
    },
  };

  // ============================================================
  //  CASE B — 原料药 HPLC 杂质超标
  // ============================================================
  const caseB = {
    id: 'DEV-2025-08-152',
    title: '原料药 HPLC 杂质 A 超标 (0.32% > 限度 0.25%)',
    type: 'LIMS + 工艺参数',
    complexity: 2,
    severity: 'major',
    status: 'reviewing',
    statusLabel: 'QA 审核中',
    discoveredAt: '2025-08-16 11:20',
    location: 'API 车间 · 反应釜 R-204',
    area: '原料药合成',
    shift: '白班',
    deadline: '2025-08-30',
    investigator: 'U-SM',
    qaReviewer: 'U-DL',
    productImpact: '批次 DS-2025-081 ~ DS-2025-083 (3批)',
    description: 'HPLC 检测杂质 A 含量 0.32%，超过放行限度 0.25%。8天趋势显示持续上升。',
    progress: 85,
    hitlGates: { gate1: 'done', gate2: 'done', gate3: 'done', gate4: 'done', gate5: 'pending' },
    timeline: [
      { id: 'T1', time: '2025-08-09 14:00', source: 'LIMS', title: '杂质 A 0.08%', detail: '正常基线', severity: 'info' },
      { id: 'T2', time: '2025-08-11 14:00', source: 'LIMS', title: '杂质 A 0.12%', detail: '轻微上升', severity: 'warning' },
      { id: 'T3', time: '2025-08-13 14:00', source: 'LIMS', title: '杂质 A 0.18%', detail: '持续上升', severity: 'warning' },
      { id: 'T4', time: '2025-08-14 10:00', source: 'CMMS', title: '色谱柱维护', detail: '柱效下降，更换色谱柱', severity: 'info' },
      { id: 'T5', time: '2025-08-15 14:00', source: 'LIMS', title: '杂质 A 0.24%', detail: '接近限度', severity: 'warning' },
      { id: 'T6', time: '2025-08-16 11:20', source: 'LIMS', title: '杂质 A 0.32% ⚠', detail: '超过限度 0.25%', severity: 'danger', anomaly: true },
    ],
    evidence: [
      { id: 'E-LIMS-10', source: 'LIMS', timestamp: '2025-08-16T11:20:00+08:00', confidence: 4, summary: 'HPLC 杂质 A 0.32%，超过限度 0.25%', hash: fakeHash('E-LIMS-10'), raw: { batch: 'DS-2025-081', method: 'HPLC-UV', impurity: 'A', result: 0.32, limit: 0.25, unit: '%' } },
      { id: 'E-MES-10', source: 'MES', timestamp: '2025-08-15T22:00:00+08:00', confidence: 3, summary: '反应温度波动 ±1.5°C (工艺范围 ±1.0°C)', hash: fakeHash('E-MES-10'), raw: { batch: 'DS-2025-081', parameter: 'reaction_temp', setpoint: 65, actual_min: 63.5, actual_max: 66.5, range_spec: 1.0, deviation: true } },
      { id: 'E-CMMS-10', source: 'CMMS', timestamp: '2025-08-14T10:00:00+08:00', confidence: 3, summary: '色谱柱老化更换 (使用 412 次超期)', hash: fakeHash('E-CMMS-10'), raw: { work_order: 'WO-20250814-0312', equipment: 'HPLC-007', component: 'column_C18', type: 'preventive', usage_count: 412, max_usage: 400, status: 'completed' } },
    ],
    fiveWhys: [
      { n: 1, why: '为什么杂质 A 超标？', answer: '反应温度波动导致副反应增加', evidence: ['E-MES-10'], confidence: 3, status: 'accepted' },
      { n: 2, why: '为什么反应温度波动？', answer: '反应釜 R-204 夹套冷却介质流量不稳', evidence: ['E-MES-10'], confidence: 2, status: 'accepted' },
      { n: 3, why: '为什么冷却介质流量不稳？', answer: '冷却循环泵 P-12 叶轮磨损 (待维修)', evidence: [], confidence: 2, status: 'pending', isRoot: true },
    ],
    emTrend: { labels: ['8/9','8/11','8/13','8/15','8/16'], act12: [0.08,0.12,0.18,0.24,0.32], act08: [], act15: [], alertLine: 0.15, actionLine: 0.25 },
  };

  // ============================================================
  //  CASE C — 无菌灌装区尘埃粒子告警
  // ============================================================
  const caseC = {
    id: 'DEV-2025-08-149',
    title: '无菌灌装区 0.5μm 尘埃粒子连续告警',
    type: 'EMS + HVAC + 维护',
    complexity: 2,
    severity: 'minor',
    status: 'investigating',
    statusLabel: '调查中',
    discoveredAt: '2025-08-16 03:15',
    location: '灌装线 F-301 · ISO 5 区',
    area: '无菌灌装',
    shift: '夜班',
    deadline: '2025-08-29',
    investigator: 'U-LC',
    qaReviewer: 'U-JW',
    productImpact: '批次 FG-2025-301 (1批) 在线监控',
    description: 'ISO 5 灌装区 0.5μm 粒子连续 3 小时超过警戒线，新风阀故障工单待处理。',
    progress: 45,
    hitlGates: { gate1: 'done', gate2: 'pending', gate3: 'pending', gate4: 'pending', gate5: 'pending' },
    timeline: [
      { id: 'T1', time: '2025-08-16 00:00', source: 'EMS', title: '粒子基线', detail: '0.5μm: 1200/m³ 正常', severity: 'info' },
      { id: 'T2', time: '2025-08-16 01:00', source: 'EMS', title: '粒子上升', detail: '0.5μm: 2800/m³ 警戒', severity: 'warning' },
      { id: 'T3', time: '2025-08-16 02:00', source: 'EMS', title: '粒子持续上升', detail: '0.5μm: 3500/m³', severity: 'warning' },
      { id: 'T4', time: '2025-08-16 03:15', source: 'EMS', title: '粒子告警 ⚠', detail: '0.5μm: 4200/m³ 持续3h', severity: 'danger', anomaly: true },
      { id: 'T5', time: '2025-08-16 03:30', source: 'CMMS', title: '新风阀故障', detail: 'WO 派工 — 阀门卡涩', severity: 'warning' },
    ],
    evidence: [
      { id: 'E-EMS-20', source: 'EMS', timestamp: '2025-08-16T03:15:00+08:00', confidence: 4, summary: '0.5μm 粒子 4200/m³ 持续 3h 告警', hash: fakeHash('E-EMS-20'), raw: { point: 'FILL-ISO5-01', size: '0.5um', count: 4200, alert: 3500, action: 10000, duration_h: 3 } },
      { id: 'E-CMMS-20', source: 'CMMS', timestamp: '2025-08-16T03:30:00+08:00', confidence: 3, summary: '新风阀 DMP-301 卡涩 — 待维修', hash: fakeHash('E-CMMS-20'), raw: { work_order: 'WO-20250816-0501', equipment: 'DMP-301', type: 'corrective', detail: '阀门执行器卡涩', status: 'open', priority: 'high' } },
    ],
    fiveWhys: [
      { n: 1, why: '为什么尘埃粒子告警？', answer: '新风补充不足，正压维持失败', evidence: ['E-EMS-20'], confidence: 3, status: 'accepted' },
      { n: 2, why: '为什么新风不足？', answer: '新风阀 DMP-301 卡涩', evidence: ['E-CMMS-20'], confidence: 3, status: 'pending', isRoot: true },
    ],
  };

  // ============================================================
  //  CASE D — 批生产记录签名缺失
  // ============================================================
  const caseD = {
    id: 'DEV-2025-08-150',
    title: '3 批 MES 批记录电子签名缺失',
    type: 'MES + 培训',
    complexity: 1,
    severity: 'minor',
    status: 'closed',
    statusLabel: '已关闭',
    discoveredAt: '2025-08-15 09:00',
    location: '包装线 P-102',
    area: '包装',
    shift: '白班',
    deadline: '2025-08-22',
    investigator: 'U-SM',
    qaReviewer: 'U-DL',
    productImpact: '批次 PK-2025-015 ~ 017 (3批)',
    description: '3 批包装记录的关键工艺步骤缺少第二人复核电子签名，已补充签名并关闭。',
    progress: 100,
    hitlGates: { gate1: 'done', gate2: 'done', gate3: 'done', gate4: 'done', gate5: 'done' },
    timeline: [
      { id: 'T1', time: '2025-08-15 09:00', source: 'MES', title: '签名缺失发现', detail: 'QA 抽查发现', severity: 'warning' },
      { id: 'T2', time: '2025-08-15 14:00', source: 'LMS', title: '操作员培训核查', detail: '权限变更未培训', severity: 'info' },
      { id: 'T3', time: '2025-08-16 10:00', source: 'MES', title: '签名补签', detail: '3批完成补签', severity: 'info' },
    ],
    evidence: [
      { id: 'E-MES-30', source: 'MES', timestamp: '2025-08-15T09:00:00+08:00', confidence: 4, summary: '3 批包装记录缺第二人签名', hash: fakeHash('E-MES-30'), raw: { batches: ['PK-2025-015','PK-2025-016','PK-2025-017'], missing: 'second_review_signature', steps: ['sealing','labeling'] } },
    ],
    fiveWhys: [
      { n: 1, why: '为什么签名缺失？', answer: '操作员权限变更后系统未强制双人复核', evidence: ['E-MES-30'], confidence: 4, status: 'accepted', isRoot: true },
    ],
  };

  // ============================================================
  //  All cases
  // ============================================================
  const cases = [caseA, caseB, caseC, caseD];

  // ============================================================
  //  Dashboard KPIs & trends
  // ============================================================
  const dashboard = {
    kpis: [
      { id: 'open', label: '未解决偏差', value: 14, delta: -3, deltaLabel: '较上周', trend: 'down', icon: 'inbox', color: 'brand' },
      { id: 'major', label: '本月严重偏差', value: 5, delta: 1, deltaLabel: '较上月', trend: 'up', trendBad: true, icon: 'alert-octagon', color: 'danger' },
      { id: 'cycle', label: '平均调查周期', value: '2.3', unit: '天', delta: -32, deltaLabel: '↓ 较 Q1', trend: 'down', icon: 'timer', color: 'success' },
      { id: 'capa', label: 'CAPA 逾期率', value: '4.2', unit: '%', delta: -1.8, deltaLabel: '较上月', trend: 'down', icon: 'repeat', color: 'warning' },
    ],
    // 30-day trend (stacked by dept)
    deviationTrend: {
      labels: Array.from({ length: 30 }, (_, i) => `8/${i + 1}`),
      depts: ['微生物', '化学分析', '生产', '包装', '工程'],
      data: {
        '微生物': [1,0,1,2,1,0,1,1,2,1,0,1,2,1,1,2,3,1,0,1,1,2,1,0,1,2,1,1,0,1],
        '化学分析': [0,1,0,1,0,1,1,0,1,0,1,0,1,1,0,1,1,2,1,0,1,0,1,1,0,1,0,1,1,0],
        '生产': [1,1,0,1,1,0,1,1,0,1,1,1,0,1,1,0,1,1,0,1,1,0,1,1,0,1,1,0,1,1],
        '包装': [0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1],
        '工程': [0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0],
      },
    },
    // Heatmap: room x deviation type
    heatmap: {
      rooms: ['Room-3B-Act-12','Room-3B-Act-08','Room-3B-Act-15','Fill-ISO5-01','R-204','P-102','Room-2A-03'],
      types: ['微生物超标','尘埃粒子','杂质超标','签名缺失','差压异常','清洁缺陷'],
      matrix: [
        [8, 1, 0, 2, 5, 3],
        [2, 1, 0, 0, 1, 1],
        [1, 0, 0, 0, 1, 1],
        [3, 7, 0, 1, 2, 0],
        [0, 0, 5, 0, 1, 0],
        [1, 0, 0, 4, 0, 1],
        [2, 1, 0, 1, 1, 2],
      ],
    },
    todos: [
      { id: 'TD1', caseId: 'DEV-2025-08-147', title: '审阅 Skeptic 反例 (SK-2)', severity: 'critical', due: '今日 18:00', type: 'hitl' },
      { id: 'TD2', caseId: 'DEV-2025-08-147', title: '确认 Why 4/5 推理链', severity: 'major', due: '今日', type: 'hitl' },
      { id: 'TD3', caseId: 'DEV-2025-08-152', title: 'QA 审阅报告 v2', severity: 'major', due: '明日', type: 'review' },
      { id: 'TD4', caseId: 'DEV-2025-08-149', title: '补充 HVAC 维护证据', severity: 'minor', due: '8/19', type: 'evidence' },
      { id: 'TD5', caseId: 'DEV-2025-08-147', title: '上传 PCR 报告签收', severity: 'major', due: '今日', type: 'sign' },
    ],
    activities: [
      { id: 'A1', user: 'Lisa Chen', avatar: 'LC', action: '提交了', target: 'DEV-147 报告 v2', time: '14:30', type: 'submit' },
      { id: 'A2', user: 'James Wilson', avatar: 'JW', action: '批准了', target: 'DEV-142', time: '13:15', type: 'approve' },
      { id: 'A3', user: 'Skeptic Agent', avatar: 'AI', action: '提出反例', target: 'DEV-147 SK-2', time: '12:48', type: 'challenge' },
      { id: 'A4', user: 'Sarah Martinez', avatar: 'SM', action: '新建', target: 'DEV-152', time: '11:20', type: 'create' },
      { id: 'A5', user: 'Reasoner Agent', avatar: 'AI', action: '更新推理', target: 'DEV-147 Why 5', time: '10:55', type: 'ai' },
      { id: 'A6', user: 'Robert Kim', avatar: 'RK', action: '查看了仪表盘', target: '8月趋势', time: '09:00', type: 'view' },
    ],
  };

  // ============================================================
  //  Inbox list (smart-sorted)
  // ============================================================
  const inboxItems = [
    { id: 'DEV-2025-08-147', title: 'B 级背景微生物污染 — M. luteus 超标', severity: 'major', status: 'investigating', dept: '微生物', investigator: 'Lisa Chen', discoveredAt: '2025-08-17 06:00', deadline: '2025-08-31', progress: 68, daysLeft: 14, score: 0.92, systems: ['EMS','LMS','BMS','MES','CMMS','LIMS'] },
    { id: 'DEV-2025-08-152', title: '原料药 HPLC 杂质 A 超标 0.32%', severity: 'major', status: 'reviewing', dept: '化学分析', investigator: 'Sarah Martinez', discoveredAt: '2025-08-16 11:20', deadline: '2025-08-30', progress: 85, daysLeft: 15, score: 0.78, systems: ['LIMS','MES','CMMS'] },
    { id: 'DEV-2025-08-149', title: '无菌灌装区尘埃粒子连续告警', severity: 'minor', status: 'investigating', dept: '微生物', investigator: 'Lisa Chen', discoveredAt: '2025-08-16 03:15', deadline: '2025-08-29', progress: 45, daysLeft: 13, score: 0.61, systems: ['EMS','CMMS','BMS'] },
    { id: 'DEV-2025-08-150', title: '3 批 MES 批记录签名缺失', severity: 'minor', status: 'closed', dept: '包装', investigator: 'Sarah Martinez', discoveredAt: '2025-08-15 09:00', deadline: '2025-08-22', progress: 100, daysLeft: 0, score: 0.20, systems: ['MES','LMS'] },
    { id: 'DEV-2025-08-142', title: '冻干机 CIP 周期异常', severity: 'minor', status: 'closed', dept: '工程', investigator: 'Lisa Chen', discoveredAt: '2025-08-10 14:00', deadline: '2025-08-24', progress: 100, daysLeft: 0, score: 0.15, systems: ['CMMS','MES'] },
    { id: 'DEV-2025-08-145', title: '纯化水 TOC 超趋势', severity: 'minor', status: 'closed', dept: '化学分析', investigator: 'Sarah Martinez', discoveredAt: '2025-08-13 10:00', deadline: '2025-08-27', progress: 100, daysLeft: 0, score: 0.12, systems: ['LIMS','BMS'] },
  ];

  // ============================================================
  //  Audit logs (hash chain)
  // ============================================================
  let _prevHash = '0000000000000000000000000000000000000000';
  const auditLogs = [
    { operator: 'system', action: 'CREATE_DEVIATION', target: 'DEV-2025-08-147', detail: 'EMS 自动告警触发偏差创建', ip: '10.0.1.12', time: '2025-08-17T06:00:01+08:00', evidenceRefs: ['E-EMS-05'] },
    { operator: 'lisa.chen@biopharma.com', action: 'LOGIN', target: '—', detail: '调查员登录', ip: '192.168.1.45', time: '2025-08-17T06:05:00+08:00', evidenceRefs: [] },
    { operator: 'Evidence Hunter', action: 'AGENT_QUERY', target: '6 systems', detail: '跨 6 系统拉取 Room-3B 近 10 天数据', ip: 'internal', time: '2025-08-17T06:10:00+08:00', evidenceRefs: ['E-EMS-01','E-EMS-02','E-EMS-03','E-EMS-04','E-EMS-05','E-LIMS-01','E-CCTV-01','E-LMS-01','E-BMS-01','E-BMS-02','E-CMMS-01'] },
    { operator: 'lisa.chen@biopharma.com', action: 'CONTEXT_INPUT', target: 'DEV-147', detail: '补充上下文: "8/12 以来同一房间第 5 次"', ip: '192.168.1.45', time: '2025-08-17T06:15:00+08:00', evidenceRefs: [] },
    { operator: 'lisa.chen@biopharma.com', action: 'CONFIRM_EVIDENCE', target: 'timeline', detail: '证据时间线确认完整 (HITL 关卡 1 通过)', ip: '192.168.1.45', time: '2025-08-17T06:30:00+08:00', evidenceRefs: [] },
    { operator: 'Reasoner', action: 'AGENT_OUTPUT', target: 'fishbone + 5whys v1', detail: '输出鱼骨图 6M + 5 Whys 初稿', ip: 'internal', time: '2025-08-17T06:45:00+08:00', evidenceRefs: [], aiModel: 'claude-sonnet-4-20250514', aiOutputHash: fakeHash('reasoner-v1') },
    { operator: 'lisa.chen@biopharma.com', action: 'EDIT_5WHY', target: 'Why 3', detail: '手动修改 Why 3: "为什么 EMP-2187 更衣不合规?"', ip: '192.168.1.45', time: '2025-08-17T07:00:00+08:00', evidenceRefs: ['E-LMS-01'] },
    { operator: 'lisa.chen@biopharma.com', action: 'ACCEPT_AI_OUTPUT', target: '5WHY-1,2,3', detail: '接受 Why 1-3 (HITL 关卡 2)', ip: '192.168.1.45', time: '2025-08-17T07:15:00+08:00', evidenceRefs: ['E-LIMS-01','E-CCTV-01','E-LMS-01'], aiModel: 'claude-sonnet-4-20250514', aiOutputHash: fakeHash('reasoner-v1') },
    { operator: 'Skeptic', action: 'AGENT_CHALLENGE', target: 'Why 2, Why 5', detail: '提出 2 条反例挑战 (SK-1, SK-2)', ip: 'internal', time: '2025-08-17T07:48:00+08:00', evidenceRefs: ['E-BMS-02','E-BMS-05','E-CMMS-03'], aiModel: 'claude-sonnet-4-20250514', aiOutputHash: fakeHash('skeptic-v1') },
    { operator: 'lisa.chen@biopharma.com', action: 'UPLOAD_EVIDENCE', target: 'PCR report', detail: '上传 PCR 鉴定报告 PDF', ip: '192.168.1.45', time: '2025-08-17T08:00:00+08:00', evidenceRefs: ['E-LIMS-01'] },
    { operator: 'Reasoner', action: 'AGENT_OUTPUT', target: 'fishbone + 5whys v2', detail: '采纳 Skeptic SK-2 后重新推理，更新根因', ip: 'internal', time: '2025-08-17T08:30:00+08:00', evidenceRefs: ['E-CMMS-03','E-BMS-05'], aiModel: 'claude-sonnet-4-20250514', aiOutputHash: fakeHash('reasoner-v2') },
    { operator: 'Drafter', action: 'AGENT_OUTPUT', target: 'report v2', detail: '生成 ICH Q9 报告草稿 v2', ip: 'internal', time: '2025-08-17T09:00:00+08:00', evidenceRefs: [], aiModel: 'claude-sonnet-4-20250514', aiOutputHash: fakeHash('drafter-v2') },
    { operator: 'lisa.chen@biopharma.com', action: 'EDIT_REPORT', target: 'sections 1,4,6', detail: '编辑报告第 1/4/6 节', ip: '192.168.1.45', time: '2025-08-17T14:30:00+08:00', evidenceRefs: [] },
    { operator: 'Guardian', action: 'AGENT_GUARD', target: 'E-LIMS-01', detail: '提示: PCR 报告未签收，需手动确认', ip: 'internal', time: '2025-08-17T14:35:00+08:00', evidenceRefs: ['E-LIMS-01'], aiModel: 'gpt-4o-mini', aiOutputHash: fakeHash('guardian-1') },
  ].map((log, i) => {
    const logId = `LOG-2025-08-17T${log.time.split('T')[1].slice(0, 8).replace(/:/g, '')}-${String(i + 1).padStart(4, '0')}`;
    const data = JSON.stringify({ ...log, prev: _prevHash });
    const hash = fakeHash(data);
    const enriched = { ...log, log_id: logId, prev_hash: _prevHash, hash, seq: i + 1 };
    _prevHash = hash;
    return enriched;
  });

  // ============================================================
  //  Knowledge base (V2 preview)
  // ============================================================
  const knowledge = {
    rootCausePatterns: [
      { id: 'RC1', pattern: '资质/培训门控缺失', count: 7, trend: 'up', cases: ['DEV-147','DEV-098','DEV-103'], capaEffectiveness: 62, category: '人员' },
      { id: 'RC2', pattern: 'HVAC 滤芯/差压异常', count: 12, trend: 'flat', cases: ['DEV-147','DEV-142','DEV-128'], capaEffectiveness: 78, category: '设备' },
      { id: 'RC3', pattern: '清洁程序执行偏差', count: 9, trend: 'down', cases: ['DEV-103','DEV-091'], capaEffectiveness: 85, category: '方法' },
      { id: 'RC4', pattern: '设备预防维护逾期', count: 6, trend: 'up', cases: ['DEV-149','DEV-142'], capaEffectiveness: 71, category: '设备' },
      { id: 'RC5', pattern: '系统间数据不联动', count: 4, trend: 'up', cases: ['DEV-147','DEV-150'], capaEffectiveness: 55, category: '系统' },
      { id: 'RC6', pattern: '更衣合规性下降', count: 8, trend: 'flat', cases: ['DEV-147','DEV-105'], capaEffectiveness: 68, category: '人员' },
      { id: 'RC7', pattern: '工艺参数漂移', count: 5, trend: 'down', cases: ['DEV-152','DEV-130'], capaEffectiveness: 82, category: '方法' },
      { id: 'RC8', pattern: '采样方法变更未评估', count: 3, trend: 'flat', cases: ['DEV-098'], capaEffectiveness: 90, category: '测量' },
    ],
    clusters: [
      { id: 'CL1', name: '人员相关 (培训/更衣)', count: 15, color: '#D72638', x: 0.3, y: 0.4, cases: ['DEV-147','DEV-103','DEV-105'] },
      { id: 'CL2', name: '设备/HVAC 相关', count: 18, color: '#F59E0B', x: 0.6, y: 0.3, cases: ['DEV-147','DEV-142','DEV-149'] },
      { id: 'CL3', name: '方法/程序相关', count: 14, color: '#0E7C66', x: 0.5, y: 0.6, cases: ['DEV-152','DEV-091'] },
      { id: 'CL4', name: '系统/数据完整性', count: 7, color: '#0B5FFF', x: 0.7, y: 0.7, cases: ['DEV-147','DEV-150'] },
      { id: 'CL5', name: '材料/供应商', count: 5, color: '#7C3AED', x: 0.2, y: 0.7, cases: ['DEV-098'] },
    ],
    capaEffectiveness: {
      labels: ['培训再认证','设备维护升级','SOP修订','系统联动API','CCTV AI识别','供应商审计'],
      current: [62, 78, 85, 55, 70, 90],
      target: [90, 90, 90, 90, 90, 90],
    },
  };

  // ============================================================
  //  Admin: RBAC + system integrations
  // ============================================================
  const admin = {
    roles: [
      { id: 'admin', name: '系统管理员', users: 2, permissions: { all: true }, color: 'danger' },
      { id: 'investigator', name: '调查员', users: 8, permissions: { investigate: true, evidence: true, draft: true, sign: 'submit' }, color: 'brand' },
      { id: 'qa_reviewer', name: 'QA 审核员', users: 4, permissions: { review: true, approve: true, sign: 'approve', audit: true }, color: 'success' },
      { id: 'ops_manager', name: '运营经理', users: 3, permissions: { dashboard: true, reports: true, readonly: true }, color: 'warning' },
      { id: 'auditor', name: '审计员 (只读)', users: 1, permissions: { audit: true, readonly: true, export: true }, color: 'ink' },
    ],
    permissionMatrix: [
      { resource: '偏差调查', admin: 'RW', investigator: 'RW', qa_reviewer: 'R', ops_manager: 'R', auditor: 'R' },
      { resource: '证据库', admin: 'RW', investigator: 'R', qa_reviewer: 'R', ops_manager: '—', auditor: 'R' },
      { resource: '报告草稿', admin: 'RW', investigator: 'RW', qa_reviewer: 'R', ops_manager: 'R', auditor: 'R' },
      { resource: '电子签名', admin: '—', investigator: '提交', qa_reviewer: '批准', ops_manager: '—', auditor: '—' },
      { resource: '审计日志', admin: 'R', investigator: '—', qa_reviewer: 'R', ops_manager: '—', auditor: 'R' },
      { resource: '用户管理', admin: 'RW', investigator: '—', qa_reviewer: '—', ops_manager: '—', auditor: '—' },
      { resource: '系统配置', admin: 'RW', investigator: '—', qa_reviewer: '—', ops_manager: '—', auditor: '—' },
    ],
    systemIntegrations: [
      { id: 'LIMS', status: 'healthy', latency: 120, lastSync: '2 分钟前', records: 124582, connector: 'v2.1.3', readonly: true },
      { id: 'MES',  status: 'healthy', latency: 95,  lastSync: '1 分钟前', records: 89241, connector: 'v2.1.3', readonly: true },
      { id: 'EMS',  status: 'healthy', latency: 78,  lastSync: '实时', records: 451208, connector: 'v2.2.0', readonly: true },
      { id: 'CMMS', status: 'degraded', latency: 850, lastSync: '5 分钟前', records: 12803, connector: 'v2.0.1', readonly: true },
      { id: 'LMS',  status: 'healthy', latency: 110, lastSync: '3 分钟前', records: 3847, connector: 'v2.1.0', readonly: true },
      { id: 'BMS',  status: 'healthy', latency: 65,  lastSync: '实时', records: 982104, connector: 'v2.2.1', readonly: true },
    ],
    agentModels: [
      { agent: 'Meta-Controller', model: 'claude-sonnet-4-20250514', role: '规划/路由', tokens: 0, cost: 0 },
      { agent: 'Evidence Hunter', model: 'tool-use (无 LLM)', role: 'API 查询', tokens: 0, cost: 0 },
      { agent: 'Reasoner', model: 'claude-sonnet-4-20250514', role: '推理', tokens: 0, cost: 0 },
      { agent: 'Drafter', model: 'claude-sonnet-4-20250514', role: '报告生成', tokens: 0, cost: 0 },
      { agent: 'Skeptic', model: 'claude-sonnet-4-20250514', role: '反例挑战', tokens: 0, cost: 0 },
      { agent: 'Guardian', model: 'gpt-4o-mini', role: '合规检查', tokens: 0, cost: 0 },
    ],
    signaturePolicy: {
      submitThreshold: 'investigator',
      approveThreshold: 'qa_reviewer',
      dualSignature: true,
      meaningOfSignature: '我确认本电子签名等同于手写签名，所提交内容真实准确',
      retentionYears: 10,
    },
  };

  // ============================================================
  //  Notifications
  // ============================================================
  const notifications = [
    { id: 'N1', type: 'danger', icon: 'siren', title: 'Skeptic 提出关键反例', body: 'DEV-147 的根因结论被挑战 (SK-2)', time: '12:48', action: { page: 'investigation', caseId: 'DEV-2025-08-147' } },
    { id: 'N2', type: 'warning', icon: 'clock', title: 'PCR 报告待签收', body: 'Guardian 提示 E-LIMS-01 需手动确认', time: '14:35', action: { page: 'investigation', caseId: 'DEV-2025-08-147' } },
    { id: 'N3', type: 'brand', icon: 'file-check', title: 'James Wilson 批准了报告', body: 'DEV-142 已归档', time: '13:15', action: { page: 'report' } },
    { id: 'N4', type: 'warning', icon: 'wrench', title: 'CMMS 连接延迟', body: '延迟 850ms，已降级', time: '11:00', action: { page: 'admin' } },
    { id: 'N5', type: 'brand', icon: 'sparkles', title: 'Reasoner 更新了推理', body: 'DEV-147 Why 5 根因已修订', time: '10:55', action: { page: 'investigation', caseId: 'DEV-2025-08-147' } },
  ];

  // ---- Command bar commands ----
  const commands = [
    { id: 'cmd-dashboard', title: '仪表盘', subtitle: '查看偏差趋势与 KPI', icon: 'layout-dashboard', category: 'nav', action: { page: 'dashboard' }, shortcut: 'G D' },
    { id: 'cmd-inbox', title: '调查收件箱', subtitle: '查看所有偏差', icon: 'inbox', category: 'nav', action: { page: 'inbox' }, shortcut: 'G I' },
    { id: 'cmd-investigation', title: '调查工作台', subtitle: '当前案例调查', icon: 'microscope', category: 'nav', action: { page: 'investigation' }, shortcut: 'G W' },
    { id: 'cmd-evidence', title: '证据库', subtitle: '跨案例证据搜索', icon: 'folder-search', category: 'nav', action: { page: 'evidence' }, shortcut: 'G E' },
    { id: 'cmd-report', title: '报告预览', subtitle: '查看报告草稿', icon: 'file-text', category: 'nav', action: { page: 'report' }, shortcut: 'G R' },
    { id: 'cmd-audit', title: '审计追踪', subtitle: '不可篡改操作日志', icon: 'shield', category: 'nav', action: { page: 'audit' }, shortcut: 'G A' },
    { id: 'cmd-knowledge', title: '知识库', subtitle: '根因模式与 CAPA 效力', icon: 'brain-circuit', category: 'nav', action: { page: 'knowledge' }, shortcut: 'G K' },
    { id: 'cmd-admin', title: '管理与合规', subtitle: 'RBAC · 系统集成 · 模型', icon: 'settings', category: 'nav', action: { page: 'admin' }, shortcut: 'G S' },
    { id: 'cmd-new-dev', title: '新建偏差调查', subtitle: '创建新的 Deviation', icon: 'plus-circle', category: 'action', action: { fn: 'newDeviation' }, shortcut: 'N' },
    { id: 'cmd-caseA', title: 'DEV-2025-08-147', subtitle: 'B 级微生物污染 (Case A)', icon: 'alert-octagon', category: 'case', action: { page: 'investigation', caseId: 'DEV-2025-08-147' }, shortcut: 'C A' },
    { id: 'cmd-caseB', title: 'DEV-2025-08-152', subtitle: 'HPLC 杂质超标 (Case B)', icon: 'flask-conical', category: 'case', action: { page: 'investigation', caseId: 'DEV-2025-08-152' }, shortcut: 'C B' },
    { id: 'cmd-caseC', title: 'DEV-2025-08-149', subtitle: '尘埃粒子告警 (Case C)', icon: 'thermometer', category: 'case', action: { page: 'investigation', caseId: 'DEV-2025-08-149' }, shortcut: 'C C' },
    { id: 'cmd-caseD', title: 'DEV-2025-08-150', subtitle: '签名缺失 (Case D)', icon: 'signature', category: 'case', action: { page: 'investigation', caseId: 'DEV-2025-08-150' }, shortcut: 'C D' },
  ];

  // ---- Public API ----
  return {
    users, sourceSystems, cases, caseA, caseB, caseC, caseD,
    dashboard, inboxItems, auditLogs, knowledge, admin,
    notifications, commands, fakeHash,
    getCase(id) { return cases.find(c => c.id === id); },
    getUser(id) { return users.find(u => u.id === id); },
  };
})();

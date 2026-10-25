import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// 写路径压测（P3-③）：创建/更新案例 → 拉取证据 → CAPA 创建 → 报告版本。
// 注意：写入会真实落库并在审计链留痕 —— 仅对测试环境执行，勿打生产。
// 用法：K6_USER=xxx K6_PASS=xxx BASE_URL=http://localhost:8080 k6 run deploy/k6/write-path.js
// 若目标环境开启了登录限流（login 429），降低 VU 或在脚本外预热 token。

const errorRate = new Rate('errors');
const createCaseLatency = new Trend('create_case_latency');

export const options = {
  // 写路径吞吐远低于读路径：峰值 10 VU 即可暴露连接池/行锁/审计序列竞争
  stages: [
    { duration: '30s', target: 5 },
    { duration: '2m', target: 10 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<800'],
    errors: ['rate<0.05'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const K6_USER = __ENV.K6_USER;
const K6_PASS = __ENV.K6_PASS;

if (!K6_USER || !K6_PASS) {
  throw new Error('缺少 K6_USER / K6_PASS 环境变量。');
}

function authHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
  };
}

export default function () {
  const loginRes = http.post(`${BASE_URL}/api/v1/auth/login`, JSON.stringify({
    email: K6_USER,
    password: K6_PASS,
  }), { headers: { 'Content-Type': 'application/json' } });

  const loginOk = check(loginRes, { 'login 200': (r) => r.status === 200 });
  errorRate.add(!loginOk);
  if (!loginOk) {
    sleep(1);
    return;
  }
  const headers = authHeaders(loginRes.json('access_token'));

  // 1. 创建偏差案例（写主路径）
  const caseRes = http.post(`${BASE_URL}/api/v1/cases`, JSON.stringify({
    title: `k6 写路径压测案例 VU-${__VU} iter-${__ITER}`,
    description: 'k6 write-path 压测自动创建，验证后由测试清理流程删除或走重审',
    severity: 'minor',
    product_impact: '压测流量，无真实产品影响',
  }), { headers });
  createCaseLatency.add(caseRes.timings.duration);
  const caseOk = check(caseRes, { 'create case 201': (r) => r.status === 201 });
  errorRate.add(!caseOk);
  if (!caseOk) {
    sleep(1);
    return;
  }
  const caseId = caseRes.json('id');

  // 2. 从源系统拉取证据（写证据表 + 审计）
  const fetchRes = http.post(
    `${BASE_URL}/api/v1/evidence/fetch-from-source`,
    JSON.stringify({ case_id: caseId, source: 'LMS', limit: 5 }),
    { headers },
  );
  check(fetchRes, { 'fetch evidence ok': (r) => r.status < 300 });
  errorRate.add(fetchRes.status >= 300);

  // 3. CAPA 创建（P2 新写路径）
  const capaRes = http.post(`${BASE_URL}/api/v1/cases/${caseId}/capas`, JSON.stringify({
    title: `k6 CAPA VU-${__VU}`,
    severity: 'major',
    responsibility: 'U-LC',
  }), { headers });
  check(capaRes, { 'create capa 201': (r) => r.status === 201 });
  errorRate.add(capaRes.status !== 201);

  // 4. 案例更新（行锁写路径）
  const updRes = http.patch(`${BASE_URL}/api/v1/cases/${caseId}`, JSON.stringify({
    description: 'k6 写路径压测更新',
  }), { headers });
  check(updRes, { 'update case 200': (r) => r.status === 200 });
  errorRate.add(updRes.status !== 200);

  sleep(0.2);
}

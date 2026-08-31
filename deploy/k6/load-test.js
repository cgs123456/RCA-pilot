import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// 自定义指标
const errorRate = new Rate('errors');
const loginLatency = new Trend('login_latency');

export const options = {
  stages: [
    { duration: '30s', target: 20 },   // 30s 内爬升到 20 VU
    { duration: '1m', target: 50 },    // 1 分钟到 50 VU
    { duration: '2m', target: 100 },   // 2 分钟到 100 VU（目标 QPS）
    { duration: '5m', target: 100 },   // 保持 100 VU 5 分钟
    { duration: '30s', target: 0 },    // 30s 降到 0
  ],
  thresholds: {
    http_req_duration: ['p(99)<500'],  // 99% 请求 < 500ms
    errors: ['rate<0.05'],              // 错误率 < 5%
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';

// 凭据从环境变量注入 —— 不得把口令硬编码进脚本（历史版本硬编码了演示口令）
const K6_USER = __ENV.K6_USER;
const K6_PASS = __ENV.K6_PASS;

if (!K6_USER || !K6_PASS) {
  throw new Error(
    '缺少 K6_USER / K6_PASS 环境变量。用法：' +
    'K6_USER=xxx K6_PASS=xxx k6 run deploy/k6/load-test.js'
  );
}

export default function () {
  // 1. 登录（/api/v1 版本化前缀 —— 旧脚本缺 /v1 全部打中 SPA fallback）
  const loginRes = http.post(`${BASE_URL}/api/v1/auth/login`, JSON.stringify({
    email: K6_USER,
    password: K6_PASS,
  }), { headers: { 'Content-Type': 'application/json' } });

  loginLatency.add(loginRes.timings.duration);
  const loginOk = check(loginRes, {
    'login 200': (r) => r.status === 200,
    'has token': (r) => r.json('access_token') !== undefined,
  });
  errorRate.add(!loginOk);

  if (!loginOk) {
    sleep(1);
    return;
  }

  const token = loginRes.json('access_token');
  const headers = { Authorization: `Bearer ${token}` };

  // 2. 查询案例列表
  const casesRes = http.get(`${BASE_URL}/api/v1/cases?page=1&page_size=20`, { headers });
  check(casesRes, {
    'cases 200': (r) => r.status === 200,
  });

  // 3. 查询 dashboard KPI
  http.get(`${BASE_URL}/api/v1/cases/dashboard/kpi`, { headers });

  // 4. 查询 Agent 运行列表（流水线状态查询是高频读路径）
  http.get(`${BASE_URL}/api/v1/agent/runs?page=1&page_size=10`, { headers });

  // 5. 查询审计日志
  http.get(`${BASE_URL}/api/v1/audit/logs?page=1&page_size=10`, { headers });

  sleep(0.1);  // 模拟用户思考时间
}

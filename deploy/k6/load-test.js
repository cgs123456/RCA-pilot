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

export default function () {
  // 1. 登录
  const loginRes = http.post(`${BASE_URL}/api/auth/login`, JSON.stringify({
    email: 'lisa.chen@biopharma.com',
    password: 'rca-demo-2025',
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
  const casesRes = http.get(`${BASE_URL}/api/cases?page=1&page_size=20`, { headers });
  check(casesRes, {
    'cases 200': (r) => r.status === 200,
  });

  // 3. 查询 dashboard KPI
  http.get(`${BASE_URL}/api/cases/dashboard/kpi`, { headers });

  // 4. 查询审计日志
  http.get(`${BASE_URL}/api/audit/logs?page=1&page_size=10`, { headers });

  sleep(0.1);  // 模拟用户思考时间
}

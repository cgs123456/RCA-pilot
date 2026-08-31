/**
 * API Client — Axios 封装 + JWT 拦截器
 *
 * 401 刷新采用 single-flight：并发 401 只触发一次 refresh（后端 refresh 会轮换
 * refresh token，多次并发刷新会携带已作废的旧 token 导致误登出）。
 */
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { useAuthStore } from '@/stores/auth.store';
import type { TokenResponse } from '@/types';

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// 请求拦截器：注入 Authorization
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 模块级共享的 in-flight refresh Promise（single-flight）
let refreshInFlight: Promise<TokenResponse | null> | null = null;

// 响应拦截器：401 自动刷新 + 重试；刷新失败统一登出
api.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const status = error.response?.status;
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    if (status === 401 && !original._retry) {
      original._retry = true;
      const { refreshToken, logout } = useAuthStore.getState();

      if (!refreshToken) {
        logout();
      } else {
        try {
          // 第一个 401 发起刷新，其余等待同一个 Promise
          if (!refreshInFlight) {
            refreshInFlight = useAuthStore
              .getState()
              .refresh()
              .finally(() => {
                refreshInFlight = null;
              });
          }
          const newTokens = await refreshInFlight;
          if (newTokens) {
            original.headers!.Authorization = `Bearer ${newTokens.access_token}`;
            return api(original);
          }
          logout();
        } catch {
          logout();
        }
      }
    }

    // 统一错误格式
    const data = error.response?.data as { code?: string; message?: string; details?: unknown } | undefined;
    const message = data?.message || error.message || '请求失败';
    return Promise.reject({ code: data?.code || 'ERROR', message, details: data?.details, status });
  }
);

export default api;

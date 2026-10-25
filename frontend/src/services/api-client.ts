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

// 裸实例：不带任何拦截器 —— 供登出等需绕过 401 刷新/递归登出逻辑的一次性调用使用
export const bareApi = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
});

/** 统一业务错误（审查 D 组：此前 reject 裸对象，无 stack、instanceof 不可判） */
export class ApiError extends Error {
  code: string;
  status?: number;
  details?: unknown;

  constructor(message: string, code = 'ERROR', status?: number, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

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
      // clear 只清状态不发请求：此处若调 logout（内含网络调用）会因无 token 的 401 无限递归
      const { refreshToken, clear } = useAuthStore.getState();

      if (!refreshToken) {
        clear();
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
          clear();
        } catch {
          clear();
        }
      }
    }

    // 统一错误格式（后端错误体键为 detail；兼容 details）
    const data = error.response?.data as
      | { code?: string; message?: string; details?: unknown; detail?: unknown }
      | undefined;
    const message = data?.message || error.message || '请求失败';
    return Promise.reject(
      new ApiError(message, data?.code || 'ERROR', status, data?.details ?? data?.detail)
    );
  }
);

export default api;

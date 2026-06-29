/**
 * API Client — Axios 封装 + JWT 拦截器
 */
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { useAuthStore } from '@/stores/auth.store';

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

// 响应拦截器：401 自动登出 + 刷新 token
api.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const status = error.response?.status;
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    if (status === 401 && !original._retry) {
      original._retry = true;
      const { refreshToken, refresh, logout } = useAuthStore.getState();
      if (refreshToken) {
        try {
          const newTokens = await refresh();
          if (newTokens) {
            original.headers!.Authorization = `Bearer ${newTokens.access_token}`;
            return api(original);
          }
        } catch {
          logout();
        }
      } else {
        logout();
      }
    }

    // 统一错误格式
    const data = error.response?.data as any;
    const message = data?.message || error.message || '请求失败';
    return Promise.reject({ code: data?.code || 'ERROR', message, details: data?.details, status });
  }
);

export default api;

/**
 * Auth Store — Zustand
 * 管理 access/refresh token、当前用户、登录/登出
 */
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import api, { bareApi } from '@/services/api-client';
import type { TokenResponse, User } from '@/types';

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
  login: (email: string, password: string, mfaCode?: string) => Promise<TokenResponse>;
  refresh: () => Promise<TokenResponse | null>;
  logout: () => Promise<void>;
  /** 仅清空本地状态、不发任何网络请求 —— 供 401 拦截器与失效场景复用 */
  clear: () => void;
  setUser: (u: User | null) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,

      login: async (email: string, password: string, mfaCode?: string) => {
        const { data } = await api.post<TokenResponse>('/auth/login', {
          email,
          password,
          ...(mfaCode ? { mfa_code: mfaCode } : {}),
        });
        set({
          accessToken: data.access_token,
          refreshToken: data.refresh_token,
          user: data.user,
          isAuthenticated: true,
        });
        return data;
      },

      refresh: async () => {
        const rt = get().refreshToken;
        if (!rt) return null;
        try {
          // bareApi：refresh 请求自身的 401 若再进拦截器会 await 自己的 single-flight Promise 造成死锁
          const { data } = await bareApi.post<TokenResponse>('/auth/refresh', { refresh_token: rt });
          set({
            accessToken: data.access_token,
            refreshToken: data.refresh_token,
            user: data.user,
            isAuthenticated: true,
          });
          return data;
        } catch {
          get().clear();
          return null;
        }
      },

      logout: async () => {
        // 先捕获 token 并清状态：本地会话立即终止，服务端吊销结果不影响登出
        const { accessToken, clear: reset } = get();
        reset();
        if (accessToken) {
          try {
            // bareApi 绕过 401 拦截器，避免吊销校验失败再次触发刷新/登出循环
            await bareApi.post('/auth/logout', null, {
              headers: { Authorization: `Bearer ${accessToken}` },
            });
          } catch {
            // 服务端吊销失败不阻塞本地登出（access token 短 TTL 兜底）
          }
        }
      },

      clear: () => set({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false }),

      setUser: (u) => set({ user: u }),
    }),
    {
      name: 'rca-auth',
      storage: createJSONStorage(() => sessionStorage), // 关闭标签页即清除（localStorage 会被 XSS 长期窃取）
      // 仅持久化 user 展示信息；isAuthenticated 不持久化 —— token 只存内存，F5 后必须重新登录，
      // 否则恢复出"已登录无 token"假态导致全站 401
      partialize: (state) => ({
        user: state.user,
      }),
    }
  )
);

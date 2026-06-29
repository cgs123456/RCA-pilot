/**
 * Auth Store — Zustand
 * 管理 access/refresh token、当前用户、登录/登出
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import api from '@/services/api-client';
import type { TokenResponse, User } from '@/types';

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<TokenResponse>;
  refresh: () => Promise<TokenResponse | null>;
  logout: () => Promise<void>;
  setUser: (u: User | null) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,

      login: async (email: string, password: string) => {
        const { data } = await api.post<TokenResponse>('/auth/login', { email, password });
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
          const { data } = await api.post<TokenResponse>('/auth/refresh', { refresh_token: rt });
          set({
            accessToken: data.access_token,
            refreshToken: data.refresh_token,
            user: data.user,
            isAuthenticated: true,
          });
          return data;
        } catch {
          set({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
          return null;
        }
      },

      logout: async () => {
        try {
          await api.post('/auth/logout');
        } catch {
          // 忽略错误
        }
        set({ accessToken: null, refreshToken: null, user: null, isAuthenticated: false });
      },

      setUser: (u) => set({ user: u }),
    }),
    {
      name: 'rca-auth',
      partialize: (state) => ({
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        user: state.user,
        isAuthenticated: state.isAuthenticated,
      }),
    }
  )
);

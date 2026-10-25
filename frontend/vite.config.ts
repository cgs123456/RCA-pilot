import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      // P3-① 收敛后仅两个后端单元：core-service(8000, auth+case+evidence+audit+capa/报告/通知)
      // 与 agent-service(8004, LLM 长任务)；与 deploy/nginx.conf 路由口径一致。
      '/api/v1/agent': {
        target: 'http://localhost:8004',
        changeOrigin: true,
      },
      '/api/v1': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});

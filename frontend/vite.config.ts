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
      // 统一 /api/v1 前缀，按服务名路由到不同后端
      '/api/v1/auth': {
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
      '/api/v1/cases': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
      '/api/v1/dashboard': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
      '/api/v1/source-systems': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
      '/api/v1/evidence': {
        target: 'http://localhost:8003',
        changeOrigin: true,
      },
      '/api/v1/timeline': {
        target: 'http://localhost:8003',
        changeOrigin: true,
      },
      '/api/v1/agent': {
        target: 'http://localhost:8004',
        changeOrigin: true,
      },
      '/api/v1/audit': {
        target: 'http://localhost:8005',
        changeOrigin: true,
      },
      '/api/v1/lms': {
        target: 'http://localhost:8011',
        changeOrigin: true,
      },
    },
  },
});

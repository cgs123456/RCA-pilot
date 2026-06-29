/**
 * App 根组件 — 路由 + 认证守卫
 */
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth.store';
import { AppShell } from '@/components/AppShell';
import { LoginPage } from '@/pages/LoginPage';
import { DashboardPage } from '@/pages/DashboardPage';
import { InboxPage } from '@/pages/InboxPage';
import { InvestigationPage } from '@/pages/InvestigationPage';
import { AuditPage } from '@/pages/AuditPage';
import { PlaceholderPage } from '@/pages/PlaceholderPage';
import { FlaskConical, FileText, BookOpen, Settings } from 'lucide-react';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <AppShell>
              <Routes>
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/inbox" element={<InboxPage />} />
                <Route path="/investigation/:caseId" element={<InvestigationPage />} />
                <Route path="/audit" element={<AuditPage />} />
                <Route
                  path="/evidence"
                  element={
                    <PlaceholderPage
                      title="证据库"
                      description="跨源系统证据统一检索与下钻，支持按系统/案例/置信度/关键词多维筛选。"
                      icon={FlaskConical}
                    />
                  }
                />
                <Route
                  path="/report"
                  element={
                    <PlaceholderPage
                      title="报告预览"
                      description="ICH Q9 模板报告预览与 PDF 导出，支持电子签名嵌入。"
                      icon={FileText}
                    />
                  }
                />
                <Route
                  path="/knowledge"
                  element={
                    <PlaceholderPage
                      title="知识库"
                      description="基于 pgvector 的 RAG 相似历史案例检索与推荐。"
                      icon={BookOpen}
                    />
                  }
                />
                <Route
                  path="/admin"
                  element={
                    <PlaceholderPage
                      title="管理与合规"
                      description="用户管理、源系统配置、CSV 验证文档、合规检查清单。"
                      icon={Settings}
                    />
                  }
                />
                <Route path="/" element={<Navigate to="/dashboard" replace />} />
              </Routes>
            </AppShell>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

/**
 * App 根组件 — 路由（懒加载）+ 认证守卫
 */
import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate, Link } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth.store';
import { AppShell } from '@/components/AppShell';
import { FlaskConical, FileText, BookOpen, Settings, Loader2 } from 'lucide-react';

// 路由级代码分割 —— 旧版全部静态 import，首包含全部页面
const LoginPage = lazy(() =>
  import('@/pages/LoginPage').then((m) => ({ default: m.LoginPage }))
);
const DashboardPage = lazy(() =>
  import('@/pages/DashboardPage').then((m) => ({ default: m.DashboardPage }))
);
const InboxPage = lazy(() =>
  import('@/pages/InboxPage').then((m) => ({ default: m.InboxPage }))
);
const InvestigationPage = lazy(() =>
  import('@/pages/InvestigationPage').then((m) => ({ default: m.InvestigationPage }))
);
const AuditPage = lazy(() =>
  import('@/pages/AuditPage').then((m) => ({ default: m.AuditPage }))
);
const SecurityPage = lazy(() =>
  import('@/pages/SecurityPage').then((m) => ({ default: m.SecurityPage }))
);
const PlaceholderPage = lazy(() =>
  import('@/pages/PlaceholderPage').then((m) => ({ default: m.PlaceholderPage }))
);

function PageLoading() {
  return (
    <div className="flex items-center justify-center h-64 text-ink-400 gap-2">
      <Loader2 className="w-5 h-5 animate-spin" />
      <span className="text-sm">加载中...</span>
    </div>
  );
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function NotFound() {
  return (
    <div className="flex h-64 flex-col items-center justify-center gap-3 text-center">
      <p className="text-4xl font-bold text-ink-200">404</p>
      <p className="text-sm text-ink-400">页面不存在或已被移除</p>
      <Link to="/dashboard" className="text-sm text-blue-600 hover:underline">
        返回工作台
      </Link>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <Suspense fallback={<PageLoading />}>
            <LoginPage />
          </Suspense>
        }
      />

      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <AppShell>
              <Suspense fallback={<PageLoading />}>
                <Routes>
                  <Route path="/dashboard" element={<DashboardPage />} />
                  <Route path="/inbox" element={<InboxPage />} />
                  <Route path="/investigation/:caseId" element={<InvestigationPage />} />
                  <Route path="/audit" element={<AuditPage />} />
                  <Route path="/security" element={<SecurityPage />} />
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
                  <Route path="*" element={<NotFound />} />
                </Routes>
              </Suspense>
            </AppShell>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

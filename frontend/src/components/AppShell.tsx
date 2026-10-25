/**
 * App Shell — 左侧导航 + 顶栏 + 主内容区
 */
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard, Inbox, FlaskConical, FileText, ShieldCheck,
  BookOpen, Settings, LogOut, Menu, X, Search,
  ChevronRight, Activity,
} from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { cn, roleLabel } from '@/lib/utils';
import { NotificationBell } from '@/components/NotificationBell';

// 各角色可见的导航分组（仅前端视觉控制；权限硬校验在后端 RBAC）
const ROLE_NAV: Record<string, string[] | 'all'> = {
  admin: 'all',
  qa_reviewer: ['工作台', '调查', '合规'],
  investigator: ['工作台', '调查'],
  ops_manager: ['工作台'],
  auditor: ['合规'],
  operator: ['工作台'],
};

const NAV_GROUPS = [
  {
    title: '工作台',
    items: [
      { path: '/dashboard', label: '仪表盘', icon: LayoutDashboard },
      { path: '/inbox', label: '调查收件箱', icon: Inbox },
    ],
  },
  {
    title: '调查',
    items: [
      { path: '/evidence', label: '证据库', icon: FlaskConical },
      { path: '/report', label: '报告预览', icon: FileText },
    ],
  },
  {
    title: '合规',
    items: [
      { path: '/audit', label: '审计追踪', icon: ShieldCheck },
      { path: '/knowledge', label: '知识库', icon: BookOpen },
      { path: '/admin', label: '管理与合规', icon: Settings },
    ],
  },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, logout } = useAuthStore();

  const allowedGroups = ROLE_NAV[user?.role || ''] ?? ['工作台'];

  const handleLogout = async () => {
    await logout();
    // 清空 TanStack Query 缓存 —— 公用终端换账号不得看到上一用户的案例/KPI 数据
    queryClient.clear();
    navigate('/login');
  };

  return (
    <div className="flex h-screen overflow-hidden">
      {/* 左侧导航 */}
      <aside
        className={cn(
          'flex flex-col bg-ink-900 text-ink-200 transition-all duration-200',
          collapsed ? 'w-16' : 'w-60'
        )}
      >
        {/* Logo */}
        <div className="h-14 flex items-center gap-2 px-4 border-b border-ink-800">
          <div className="w-8 h-8 rounded-md bg-brand-500 flex items-center justify-center flex-shrink-0">
            <Activity className="w-5 h-5 text-white" />
          </div>
          {!collapsed && (
            <div className="flex flex-col">
              <span className="text-sm font-semibold text-white">RCA Pilot</span>
              <span className="text-[10px] text-ink-400">GxP Agentic AI</span>
            </div>
          )}
        </div>

        {/* 导航菜单 */}
        <nav className="flex-1 overflow-y-auto py-3">
          {NAV_GROUPS.filter((group) => allowedGroups === 'all' || allowedGroups.includes(group.title)).map((group) => (
            <div key={group.title} className="mb-4">
              {!collapsed && (
                <div className="px-4 mb-1.5 text-[10px] uppercase tracking-wider text-ink-500 font-semibold">
                  {group.title}
                </div>
              )}
              {group.items.map((item) => {
                const active = location.pathname.startsWith(item.path);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.path}
                    to={item.path}
                    className={cn(
                      'flex items-center gap-2.5 px-4 py-2 text-sm transition-colors',
                      'hover:bg-ink-800 hover:text-white',
                      active && 'bg-brand-600/20 text-white border-l-2 border-brand-500'
                    )}
                    title={collapsed ? item.label : undefined}
                  >
                    <Icon className="w-4 h-4 flex-shrink-0" />
                    {!collapsed && <span>{item.label}</span>}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        {/* 折叠按钮 */}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="h-10 flex items-center justify-center border-t border-ink-800 hover:bg-ink-800"
        >
          {collapsed ? <Menu className="w-4 h-4" /> : <X className="w-4 h-4" />}
        </button>
      </aside>

      {/* 右侧主区 */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* 顶栏 */}
        <header className="h-14 glass border-b border-ink-200 flex items-center justify-between px-6 flex-shrink-0">
          <div className="flex items-center gap-2 text-sm text-ink-500">
            <span>RCA Pilot</span>
            <ChevronRight className="w-3 h-3" />
            <span className="text-ink-800 font-medium">
              {NAV_GROUPS.flatMap((g) => g.items).find((i) =>
                location.pathname.startsWith(i.path)
              )?.label || '页面'}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <button className="p-1.5 rounded hover:bg-ink-100" title="搜索">
              <Search className="w-4 h-4 text-ink-500" />
            </button>
            {/* P2-⑧ 站内协作通知：未读徽章 + 下拉面板 */}
            <NotificationBell />
            <button
              onClick={() => navigate('/security')}
              className="p-1.5 rounded hover:bg-ink-100"
              title="安全设置（两步验证）"
            >
              <ShieldCheck className="w-4 h-4 text-ink-500" />
            </button>

            <div className="h-6 w-px bg-ink-200" />

            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-full bg-brand-500 flex items-center justify-center text-white text-xs font-semibold">
                {user?.avatar || user?.name?.[0] || 'U'}
              </div>
              <div className="flex flex-col">
                <span className="text-xs font-medium text-ink-800">{user?.name}</span>
                <span className="text-[10px] text-ink-500">{user && roleLabel(user.role)}</span>
              </div>
            </div>

            <button
              onClick={handleLogout}
              className="p-1.5 rounded hover:bg-danger-50 hover:text-danger-600"
              title="登出"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </header>

        {/* 主内容 */}
        <main className="flex-1 overflow-auto bg-ink-50">
          {children}
        </main>
      </div>
    </div>
  );
}

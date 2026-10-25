/**
 * 通知铃铛 — P2-⑧ 站内协作通知
 * 未读徽章 30s 轮询；点击展开近 10 条，单条点击标记已读并跳转案例
 */
import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Bell, CheckCheck } from 'lucide-react';
import { notificationApi, NOTIFICATION_TYPE_LABEL, type NotificationItem } from '@/services/notification.service';
import { cn, formatDateTime } from '@/lib/utils';
import { useAuthStore } from '@/stores/auth.store';

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const { data } = useQuery({
    queryKey: ['notifications', 'badge'],
    queryFn: () => notificationApi.list(1, 10),
    enabled: isAuthenticated,
    refetchInterval: 30_000,
  });
  const unread = data?.unread ?? 0;

  const markRead = useMutation({
    mutationFn: (id: string) => notificationApi.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  });
  const markAll = useMutation({
    mutationFn: () => notificationApi.markAllRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  });

  // 点击面板外部关闭
  useEffect(() => {
    if (!open) return;
    const onClickAway = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onClickAway);
    return () => document.removeEventListener('mousedown', onClickAway);
  }, [open]);

  const openItem = (n: NotificationItem) => {
    if (!n.read_at) markRead.mutate(n.id);
    setOpen(false);
    if (n.case_id) navigate(`/investigation/${n.case_id}`);
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="relative p-1.5 rounded hover:bg-ink-100"
        title="通知"
        aria-label={unread > 0 ? `通知，${unread} 条未读` : '通知'}
      >
        <Bell className="w-4 h-4 text-ink-500" />
        {unread > 0 && (
          <span
            data-testid="notification-badge"
            className="absolute -top-0.5 -right-0.5 min-w-4 h-4 px-1 rounded-full bg-danger-500 text-white text-[10px] leading-4 text-center font-semibold"
          >
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 top-10 z-50 w-80 rounded-lg border border-ink-200 bg-white shadow-lg"
          role="dialog"
          aria-label="通知列表"
        >
          <div className="flex items-center justify-between px-3 py-2 border-b border-ink-100">
            <span className="text-xs font-semibold text-ink-700">
              通知{unread > 0 ? `（${unread} 未读）` : ''}
            </span>
            <button
              onClick={() => markAll.mutate()}
              disabled={markAll.isPending || unread === 0}
              className="flex items-center gap-1 text-xs text-brand-600 hover:underline disabled:text-ink-400 disabled:no-underline"
            >
              <CheckCheck className="w-3.5 h-3.5" /> 全部已读
            </button>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {(data?.items ?? []).length === 0 && (
              <div className="px-3 py-6 text-center text-xs text-ink-400">暂无通知</div>
            )}
            {(data?.items ?? []).map((n) => (
              <button
                key={n.id}
                onClick={() => openItem(n)}
                className={cn(
                  'w-full text-left px-3 py-2.5 border-b border-ink-50 hover:bg-ink-50 transition-colors',
                  !n.read_at && 'bg-brand-50/60'
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-ink-800">
                    {NOTIFICATION_TYPE_LABEL[n.type] ?? n.type}
                  </span>
                  <span className="text-[10px] text-ink-400">{formatDateTime(n.created_at)}</span>
                </div>
                <div className="text-xs text-ink-500 mt-0.5 truncate">
                  {typeof n.payload?.title === 'string' ? n.payload.title : n.case_id ?? ''}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

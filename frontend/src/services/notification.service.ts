/**
 * Notification API — P2-⑧ 站内协作通知
 */
import api from './api-client';

export type NotificationType =
  | 'CASE_ASSIGNED'
  | 'GATE_READY'
  | 'CASE_CLOSED'
  | 'CAPA_CREATED';

export interface NotificationItem {
  id: string;
  type: NotificationType;
  case_id: string | null;
  payload: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationList {
  items: NotificationItem[];
  unread: number;
}

export const notificationApi = {
  list: async (page = 1, page_size = 10): Promise<NotificationList> => {
    const { data } = await api.get<NotificationList>('/notifications', {
      params: { page, page_size },
    });
    return data;
  },
  markRead: async (id: string): Promise<NotificationItem> => {
    const { data } = await api.post<NotificationItem>(`/notifications/${id}/read`);
    return data;
  },
  markAllRead: async (): Promise<{ updated: number }> => {
    const { data } = await api.post<{ updated: number }>('/notifications/read-all');
    return data;
  },
};

export const NOTIFICATION_TYPE_LABEL: Record<NotificationType, string> = {
  CASE_ASSIGNED: '案例指派',
  GATE_READY: '待人工审批',
  CASE_CLOSED: '案例已结案',
  CAPA_CREATED: '新建 CAPA',
};

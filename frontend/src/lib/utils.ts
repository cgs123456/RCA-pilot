/**
 * 工具函数
 */
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Tailwind class merge */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 格式化日期时间 */
export function formatDateTime(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${y}-${m}-${day} ${h}:${min}`;
}

/** 格式化日期（本地时区 —— 旧版 toISOString 用 UTC，+08:00 下日期可能差一天） */
export function formatDate(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** 相对时间（如 "3 天前"） */
export function timeAgo(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return '刚刚';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  const day = Math.floor(h / 24);
  if (day < 30) return `${day} 天前`;
  return formatDate(iso);
}

/** 严重等级 → 中文标签 */
export function severityLabel(s: string): string {
  return ({ critical: '严重', major: '重大', minor: '次要', info: '提示' } as Record<string, string>)[s] || s;
}

/** 严重等级 → badge class */
export function severityBadgeClass(s: string): string {
  return ({ critical: 'sev-critical', major: 'sev-major', minor: 'sev-minor', info: 'sev-info' } as Record<string, string>)[s] || 'sev-info';
}

/** 案例状态 → 中文标签 */
export function statusLabel(s: string): string {
  return ({
    pending: '待处理',
    investigating: '调查中',
    reviewing: '审核中',
    closed: '已结案',
  } as Record<string, string>)[s] || s;
}

/** 案例状态 → badge class */
export function statusBadgeClass(s: string): string {
  return ({
    pending: 'bg-ink-100 text-ink-600',
    investigating: 'bg-brand-50 text-brand-600',
    reviewing: 'bg-warning-50 text-warning-600',
    closed: 'bg-success-50 text-success-600',
  } as Record<string, string>)[s] || 'bg-ink-100 text-ink-600';
}

/** HITL 关卡 → 中文标签 */
export function gateLabel(gate: string): string {
  return ({
    gate1: '证据完整性',
    gate2: '5 Whys 审查',
    gate3: 'Skeptic 反例',
    gate4: 'CAPA 评审',
    gate5: '报告签发',
  } as Record<string, string>)[gate] || gate;
}

/** 源系统 → 中文标签 */
export function sourceLabel(code: string): string {
  return ({
    LIMS: 'LIMS',
    MES: 'MES',
    EMS: 'EMS',
    CMMS: 'CMMS',
    LMS: 'LMS',
    BMS: 'BMS',
  } as Record<string, string>)[code] || code;
}

/** 源系统 → badge class（src-${code} 形式，对应 index.css 中预定义的 src-lims/src-mes 等） */
export function sourceBadgeClass(code: string): string {
  const known = ['LIMS', 'MES', 'EMS', 'CMMS', 'LMS', 'BMS'];
  if (known.includes(code)) {
    return `src-${code.toLowerCase()}`;
  }
  // 兜底：未知源系统使用默认灰色样式
  return 'bg-ink-100 text-ink-600';
}

/** 用户角色 → 中文标签 */
export function roleLabel(role: string): string {
  return ({
    admin: '管理员',
    investigator: '调查员',
    qa_reviewer: 'QA 审核员',
    ops_manager: '运营经理',
    auditor: '审计员',
    operator: '操作员',
  } as Record<string, string>)[role] || role;
}

/** 缩短 hash 显示 */
export function shortHash(hash: string | undefined, prefix = 12): string {
  if (!hash) return '—';
  return hash.length > prefix + 6 ? `${hash.slice(0, prefix)}...${hash.slice(-6)}` : hash;
}

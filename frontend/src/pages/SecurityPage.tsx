/**
 * 安全设置页 — TOTP 两步验证注册/激活/停用（P3-④）
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ShieldCheck, ShieldOff, KeyRound, Loader2 } from 'lucide-react';
import api from '@/services/api-client';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';

interface TotpStatus {
  enabled: boolean;
  pending: boolean;
}

export function SecurityPage() {
  const queryClient = useQueryClient();
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [message, setMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const status = useQuery({
    queryKey: ['totp-status'],
    queryFn: async () => {
      const { data } = await api.get<TotpStatus>('/auth/totp/status');
      return data;
    },
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['totp-status'] });
    setSetup(null);
    setCode('');
    setPassword('');
  };

  const startSetup = useMutation({
    mutationFn: async () => {
      const { data } = await api.post<{ secret: string; otpauth_uri: string }>(
        '/auth/totp/setup'
      );
      return data;
    },
    onSuccess: (data) => {
      setSetup(data);
      setMessage({ kind: 'ok', text: '已生成密钥，请用认证器扫码或手输后输入 6 位验证码完成激活' });
    },
    onError: (e) => setMessage({ kind: 'err', text: (e as Error).message }),
  });

  const activate = useMutation({
    mutationFn: async () => {
      await api.post('/auth/totp/activate', { code });
    },
    onSuccess: () => {
      invalidate();
      setMessage({ kind: 'ok', text: '两步验证已启用，下次登录需输入动态验证码' });
    },
    onError: (e) => setMessage({ kind: 'err', text: (e as Error).message }),
  });

  const disable = useMutation({
    mutationFn: async () => {
      await api.post('/auth/totp/disable', { password });
    },
    onSuccess: () => {
      invalidate();
      setMessage({ kind: 'ok', text: '两步验证已停用' });
    },
    onError: (e) => setMessage({ kind: 'err', text: (e as Error).message }),
  });

  if (status.isLoading) {
    return (
      <div className="p-8 flex items-center gap-2 text-ink-400">
        <Loader2 className="w-4 h-4 animate-spin" /> 加载中...
      </div>
    );
  }

  const enabled = status.data?.enabled ?? false;
  const pending = status.data?.pending ?? false;

  return (
    <div className="p-8 max-w-2xl mx-auto space-y-6">
      <div className="flex items-center gap-2">
        <ShieldCheck className="w-5 h-5 text-brand-500" />
        <h1 className="text-xl font-semibold text-ink-900">安全设置</h1>
      </div>

      {message && (
        <div
          role="alert"
          className={`p-3 rounded-md text-sm ${
            message.kind === 'ok'
              ? 'bg-brand-50 text-brand-600'
              : 'bg-danger-50 text-danger-600'
          }`}
        >
          {message.text}
        </div>
      )}

      <Card className="p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="font-medium text-ink-900 flex items-center gap-2">
              <KeyRound className="w-4 h-4 text-ink-500" />
              两步验证（TOTP）
            </div>
            <p className="text-sm text-ink-500 mt-1">
              启用后登录除密码外还需输入认证器（Google Authenticator 等）的 6 位动态验证码。
            </p>
            <p className="mt-2 text-sm">
              状态：
              <span className={enabled ? 'text-green-600 font-medium' : 'text-ink-500'}>
                {enabled ? '已启用' : pending ? '注册中（未激活）' : '未启用'}
              </span>
            </p>
          </div>
        </div>

        {/* 注册流程 */}
        {!enabled && (
          <div className="mt-5 space-y-4 border-t border-ink-100 pt-5">
            {!setup ? (
              <>
                {pending && (
                  <p className="text-xs text-amber-600">
                    存在未完成的注册（旧密钥未激活不参与登录），可重新生成密钥。
                  </p>
                )}
                <Button onClick={() => startSetup.mutate()} disabled={startSetup.isPending}>
                  {startSetup.isPending ? '生成中...' : pending ? '重新生成密钥' : '开始注册'}
                </Button>
              </>
            ) : (
              <div className="space-y-3">
                <div>
                  <div className="text-xs font-medium text-ink-700 mb-1">
                    密钥（在认证器中手动添加账户时输入）
                  </div>
                  <code className="block px-3 py-2 rounded-md bg-ink-100 text-sm font-mono break-all select-all">
                    {setup.secret}
                  </code>
                </div>
                <div>
                  <div className="text-xs font-medium text-ink-700 mb-1">
                    或将以下链接粘贴到支持 otpauth 的认证器
                  </div>
                  <code className="block px-3 py-2 rounded-md bg-ink-100 text-xs font-mono break-all select-all">
                    {setup.otpauth_uri}
                  </code>
                </div>
                <div>
                  <label className="block text-xs font-medium text-ink-700 mb-1">
                    输入认证器当前 6 位验证码完成激活
                  </label>
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                    className="w-40 h-10 px-3 rounded-md border border-ink-200 bg-white text-sm tracking-[0.3em] focus:outline-none focus:ring-2 focus:ring-brand-500"
                    placeholder="000000"
                    autoComplete="one-time-code"
                  />
                </div>
                <div className="flex gap-2">
                  <Button
                    onClick={() => activate.mutate()}
                    disabled={code.length !== 6 || activate.isPending}
                  >
                    {activate.isPending ? '激活中...' : '激活'}
                  </Button>
                  <Button variant="ghost" onClick={() => setSetup(null)}>
                    取消
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 停用 */}
        {enabled && (
          <div className="mt-5 space-y-3 border-t border-ink-100 pt-5">
            <label className="block text-xs font-medium text-ink-700">
              停用需验证登录密码
            </label>
            <div className="flex gap-2">
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="flex-1 h-10 px-3 rounded-md border border-ink-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-brand-500"
                placeholder="登录密码"
                autoComplete="current-password"
              />
              <Button
                variant="danger"
                onClick={() => disable.mutate()}
                disabled={password.length < 12 || disable.isPending}
              >
                <ShieldOff className="w-4 h-4 mr-1" />
                {disable.isPending ? '停用中...' : '停用两步验证'}
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

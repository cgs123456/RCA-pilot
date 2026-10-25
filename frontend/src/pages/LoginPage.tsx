/**
 * 登录页
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Activity, Lock, Mail, AlertCircle, ShieldCheck } from 'lucide-react';
import { useAuthStore } from '@/stores/auth.store';
import { Button } from '@/components/ui/Button';

// 演示账号仅在 dev 构建存在 —— 生产 bundle 不得包含任何口令（审计发现项）
const DEMO_ACCOUNTS: { label: string; email: string; password: string }[] = import.meta.env.DEV
  ? [
      { label: '调查员 (Lisa Chen)', email: 'lisa.chen@biopharma.com', password: 'rca-demo-2025' },
      { label: 'QA 审核员 (James Wilson)', email: 'james.wilson@biopharma.com', password: 'rca-demo-2025' },
      { label: '管理员', email: 'admin@biopharma.com', password: 'rca-admin-2025' },
    ]
  : [];

export function LoginPage() {
  const navigate = useNavigate();
  const { login } = useAuthStore();
  const [email, setEmail] = useState(import.meta.env.DEV ? 'lisa.chen@biopharma.com' : '');
  const [password, setPassword] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [mfaRequired, setMfaRequired] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await login(email, password, mfaRequired ? mfaCode : undefined);
      navigate('/dashboard');
    } catch (err) {
      const apiErr = err as { message?: string; code?: string };
      // E4015：该账号已启用 TOTP 两步验证 —— 展开动态验证码输入后重试
      if (apiErr?.code === 'E4015') setMfaRequired(true);
      setError(apiErr?.message || '登录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex bg-ink-50">
      {/* 左侧品牌区 */}
      <div className="hidden lg:flex lg:w-1/2 bg-ink-900 text-white flex-col justify-between p-12 relative overflow-hidden">
        <div className="absolute inset-0 opacity-10" style={{
          backgroundImage: 'radial-gradient(circle at 30% 20%, #0B5FFF 0%, transparent 50%), radial-gradient(circle at 70% 80%, #D72638 0%, transparent 50%)',
        }} />
        <div className="relative">
          <div className="flex items-center gap-3 mb-12">
            <div className="w-10 h-10 rounded-lg bg-brand-500 flex items-center justify-center">
              <Activity className="w-6 h-6 text-white" />
            </div>
            <div>
              <div className="text-lg font-semibold">RCA Pilot</div>
              <div className="text-xs text-ink-400">GxP Agentic AI 偏差调查平台</div>
            </div>
          </div>

          <h1 className="text-4xl font-bold leading-tight mb-4">
            受 GxP 监管的<br />
            <span className="text-brand-500">Agentic AI</span> 偏差调查
          </h1>
          <p className="text-ink-400 text-sm leading-relaxed max-w-md">
            融合多 Agent 编排、HITL 人工决策门控、不可篡改审计链与 21 CFR Part 11 电子签名，
            为生物制药企业提供端到端的偏差根因分析能力。
          </p>
        </div>

        <div className="relative space-y-3">
          {[
            { icon: ShieldCheck, text: 'ALCOA+ 9 项数据完整性合规' },
            { icon: Activity, text: '6 Agent 流水线 + 5 道 HITL 关卡' },
            { icon: Lock, text: 'hash 链审计 + 双人电子签名' },
          ].map((f, i) => (
            <div key={i} className="flex items-center gap-3 text-sm text-ink-300">
              <f.icon className="w-4 h-4 text-brand-500" />
              <span>{f.text}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 右侧登录表单 */}
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-sm">
          <div className="mb-8">
            <h2 className="text-2xl font-bold text-ink-900">登录</h2>
            <p className="text-sm text-ink-500 mt-1">使用企业邮箱登录 RCA Pilot</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="flex items-start gap-2 p-3 rounded-md bg-danger-50 text-danger-600 text-sm animate-fade-in">
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            <div>
              <label className="block text-xs font-medium text-ink-700 mb-1.5">邮箱</label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-400" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full h-10 pl-10 pr-3 rounded-md border border-ink-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                  placeholder="your.name@biopharma.com"
                  required
                />
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-ink-700 mb-1.5">密码</label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-400" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full h-10 pl-10 pr-3 rounded-md border border-ink-200 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                  placeholder="••••••••"
                  required
                />
              </div>
            </div>

            {mfaRequired && (
              <div>
                <label className="block text-xs font-medium text-ink-700 mb-1.5">
                  动态验证码（认证器 6 位数字）
                </label>
                <div className="relative">
                  <ShieldCheck className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-400" />
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    value={mfaCode}
                    onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
                    className="w-full h-10 pl-10 pr-3 rounded-md border border-ink-200 bg-white text-sm tracking-[0.3em] focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    placeholder="000000"
                    autoComplete="one-time-code"
                    autoFocus
                    required
                  />
                </div>
              </div>
            )}

            <Button type="submit" size="lg" className="w-full" disabled={loading}>
              {loading ? '登录中...' : '登录'}
            </Button>
          </form>

          {DEMO_ACCOUNTS.length > 0 && (
          <div className="mt-8 pt-6 border-t border-ink-200">
            <div className="text-xs text-ink-500 mb-3">演示账号（点击快速填充）</div>
            <div className="space-y-1.5">
              {DEMO_ACCOUNTS.map((acc) => (
                <button
                  key={acc.email}
                  type="button"
                  onClick={() => {
                    setEmail(acc.email);
                    setPassword(acc.password);
                  }}
                  className="w-full text-left px-3 py-2 rounded-md hover:bg-ink-100 text-xs text-ink-600 transition-colors"
                >
                  <span className="font-medium text-ink-800">{acc.label}</span>
                  <span className="block text-ink-500 mt-0.5">{acc.email}</span>
                </button>
              ))}
            </div>
          </div>
          )}
        </div>
      </div>
    </div>
  );
}

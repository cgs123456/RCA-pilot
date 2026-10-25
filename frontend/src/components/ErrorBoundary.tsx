/**
 * 全局错误边界 — 捕获渲染期异常，避免整站白屏
 * （审查 D 组：main.tsx 无 ErrorBoundary，任何页面渲染异常都会卸载整棵树）
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台保留诊断上下文（组件栈）；不落敏感信息
    console.error('页面渲染异常:', error, info.componentStack);
  }

  private handleReload = (): void => {
    window.location.assign('/');
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div
          role="alert"
          className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-50 px-6 text-center"
        >
          <h1 className="text-xl font-semibold text-slate-800">页面出现异常</h1>
          <p className="max-w-md text-sm text-slate-500">
            界面渲染时发生未预期错误。您的数据不受影响，可返回首页或刷新重试。
          </p>
          <button
            type="button"
            onClick={this.handleReload}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            返回首页
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

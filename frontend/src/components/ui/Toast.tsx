/**
 * Toast 全局提示 — 极简实现（context + useToast）
 * 右下角堆叠，3 秒自动消失，支持 success/error
 */
import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { CheckCircle, XCircle, Info } from 'lucide-react';

type ToastKind = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastContextValue {
  toast: (message: string, kind?: ToastKind) => void;
}

const ToastContext = createContext<ToastContextValue>({ toast: () => {} });

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const toast = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = nextId.current++;
    setItems((prev) => [...prev, { id, kind, message }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((t) => t.id !== id));
    }, 3000);
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {/* Toast 渲染层 */}
      <div className="fixed bottom-4 right-4 z-[100] space-y-2 max-w-sm">
        {items.map((t) => (
          <div
            key={t.id}
            className={`flex items-start gap-2 px-3 py-2.5 rounded-md shadow-lg text-sm animate-fade-in ${
              t.kind === 'success'
                ? 'bg-success-600 text-white'
                : t.kind === 'error'
                  ? 'bg-danger-600 text-white'
                  : 'bg-ink-800 text-white'
            }`}
          >
            {t.kind === 'success' ? (
              <CheckCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            ) : t.kind === 'error' ? (
              <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            ) : (
              <Info className="w-4 h-4 mt-0.5 flex-shrink-0" />
            )}
            <span className="leading-snug">{t.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

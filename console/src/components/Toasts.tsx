// Brief confirmations and failures, announced to assistive technology.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";

interface Toast {
  id: number;
  tone: "ok" | "error" | "info";
  text: string;
}

interface Toasts {
  ok: (text: string) => void;
  info: (text: string) => void;
  error: (error: unknown) => void;
}

const ToastContext = createContext<Toasts | null>(null);
let next = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = next++;
    setItems((current) => [...current.slice(-3), { id, tone, text }]);
    window.setTimeout(() => setItems((current) => current.filter((t) => t.id !== id)), 6000);
  }, []);
  const api = useMemo<Toasts>(
    () => ({
      ok: (text) => push("ok", text),
      info: (text) => push("info", text),
      error: (error) =>
        push(
          "error",
          error instanceof ApiError
            ? `${error.explanation}${error.problem.detail && error.problem.detail !== error.explanation ? ` ${error.problem.detail}` : ""}`
            : error instanceof Error
              ? error.message
              : String(error),
        ),
    }),
    [push],
  );
  return (
    <ToastContext value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext>
  );
}

export function useToasts(): Toasts {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToasts must be used inside ToastProvider");
  return value;
}

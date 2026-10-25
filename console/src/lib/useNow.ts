// A clock for ages and countdowns.
import { useEffect, useState } from "react";

/** The current time, re-rendering every `intervalMs` (for ages and countdowns). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

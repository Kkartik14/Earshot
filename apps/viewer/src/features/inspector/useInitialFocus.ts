import { useEffect, useRef } from "react";

/** Focus a stable target when a detail view opens or changes identity. */
export function useInitialFocus<T extends HTMLElement>(focusKey: string | number) {
  const target = useRef<T>(null);

  useEffect(() => {
    target.current?.focus();
  }, [focusKey]);

  return target;
}

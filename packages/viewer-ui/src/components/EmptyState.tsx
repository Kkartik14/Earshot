import { useEffect, useRef } from "react";
import styles from "./EmptyState.module.css";

export function EmptyState({
  title,
  hint,
  focusTitle = false,
}: {
  title: string;
  hint?: string;
  focusTitle?: boolean;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusTitle) heading.current?.focus();
  }, [focusTitle]);

  return (
    <div className={styles.empty}>
      {focusTitle ? (
        <h1 ref={heading} tabIndex={-1} className={styles.title}>
          {title}
        </h1>
      ) : (
        <p className={styles.title}>{title}</p>
      )}
      {hint ? <p className={styles.hint}>{hint}</p> : null}
    </div>
  );
}

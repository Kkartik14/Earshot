/** The Earshot signal mark. Inherits color from `currentColor`. */
export function Waveform({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 52 40" fill="none" aria-hidden="true">
      <path
        d="M1 20 L15 8 L19 33 L23 14 L27 26 L30 20 H36 L40 20 L43 12 L47 28 L51 20"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

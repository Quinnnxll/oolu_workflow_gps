// The OoLu mark: two nodes joined by a route — the big O and the small
// o of the name, read as a workflow edge. One inline SVG, stroke-only,
// so it wears the app's accent in both palettes and scales anywhere
// (login card, headers, favicon) without an asset path to break.
export function OoLuMark({ size = 40 }: { size?: number }) {
  return (
    <svg
      className="oolu-mark"
      width={size}
      height={(size * 3) / 4}
      viewBox="0 0 64 48"
      role="img"
      aria-label="OoLu logo"
    >
      <circle
        cx="20"
        cy="22"
        r="13"
        fill="none"
        stroke="currentColor"
        strokeWidth="5.5"
      />
      <circle
        cx="49"
        cy="32"
        r="8"
        fill="none"
        stroke="currentColor"
        strokeWidth="5.5"
      />
      <line
        x1="32.2"
        y1="26.5"
        x2="41.5"
        y2="29.7"
        stroke="currentColor"
        strokeWidth="5.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

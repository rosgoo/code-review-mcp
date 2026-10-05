import type { DiffStyle } from "../lib/types";

const STYLES: readonly DiffStyle[] = ["unified", "split"];

export function DiffStyleToggle({
  value,
  onChange,
}: {
  value: DiffStyle;
  onChange(style: DiffStyle): void;
}) {
  return (
    <div className="segmented" role="group" aria-label="Diff layout">
      {STYLES.map((style) => (
        <button
          key={style}
          type="button"
          className={style === value ? "active" : ""}
          aria-pressed={style === value}
          onClick={() => onChange(style)}
        >
          {style === "unified" ? "Unified" : "Split"}
        </button>
      ))}
    </div>
  );
}

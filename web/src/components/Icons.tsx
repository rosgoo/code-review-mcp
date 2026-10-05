import type { CheckState } from "../lib/types";

const CI_ICONS: Record<string, { icon: string; label: string }> = {
  success: { icon: "✅", label: "CI passing" },
  failure: { icon: "❌", label: "CI failing" },
  pending: { icon: "⏳", label: "CI pending" },
  none: { icon: "—", label: "No CI checks" },
};

export function CiIcon({ state, label }: { state: CheckState | null; label?: string }) {
  const known = CI_ICONS[state ?? "none"] ?? CI_ICONS.none!;
  const text = label ?? known.label;
  return (
    <span className="ci-icon" title={text} aria-label={text} role="img">
      {known.icon}
    </span>
  );
}

export function StackIcon() {
  return (
    <svg className="stack-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        d="M8 1.5 14.5 5 8 8.5 1.5 5Z M2.6 8 8 10.9 13.4 8 14.5 8.6 8 12.1 1.5 8.6Z M2.6 10.9 8 13.8 13.4 10.9 14.5 11.5 8 15 1.5 11.5Z"
        fill="currentColor"
      />
    </svg>
  );
}

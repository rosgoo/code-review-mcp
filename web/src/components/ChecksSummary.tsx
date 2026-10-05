import { attentionChecks, countChecks } from "../lib/pr";
import type { CheckState, PrGithub } from "../lib/types";

const STATE_LABEL: Record<CheckState, string> = {
  failure: "failing",
  pending: "pending",
  success: "passing",
  skipped: "skipped",
};

function CheckDot({ state }: { state: CheckState | null }) {
  return <span className={`ci-dot ci-${state ?? "none"}`} aria-hidden="true" />;
}

export function ChecksSummary({ github }: { github: PrGithub | null }) {
  if (github === null) {
    return (
      <div className="ci-row">
        <CheckDot state={null} />
        <span className="muted">CI status loads on Refresh.</span>
      </div>
    );
  }
  const counts = countChecks(github.checks);
  const attention = attentionChecks(github.checks);
  const parts = (Object.keys(STATE_LABEL) as CheckState[])
    .filter((state) => counts[state] > 0)
    .map((state) => `${counts[state]} ${STATE_LABEL[state]}`);
  const state = github.checks_state;

  return (
    <details className="ci-row" open={state === "failure"}>
      <summary>
        <CheckDot state={state} />
        <span className="ci-state">
          {state === null ? "No CI checks" : `CI ${STATE_LABEL[state]}`}
        </span>
        {parts.length > 0 && <span className="muted">{parts.join(" · ")}</span>}
      </summary>
      {attention.length === 0 ? (
        <p className="muted ci-none">No failing or pending checks.</p>
      ) : (
        <ul className="ci-list">
          {attention.map((check) => (
            <li key={`${check.workflow ?? ""}:${check.name}`}>
              <CheckDot state={check.state} />
              {check.url ? (
                <a href={check.url} target="_blank" rel="noreferrer noopener">
                  {check.name}
                </a>
              ) : (
                check.name
              )}
              {check.workflow && <span className="muted"> · {check.workflow}</span>}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

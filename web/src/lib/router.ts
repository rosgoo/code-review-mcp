import { useSyncExternalStore } from "react";

export type Route =
  | { kind: "inbox" }
  | { kind: "review"; id: string }
  | { kind: "redirect"; to: string }
  | { kind: "not_found" };

export const reviewPath = (id: string) => `/r/${encodeURIComponent(id)}`;

export function resolveRoute(pathname: string, search: string): Route {
  if (pathname === "/") {
    const legacyId = new URLSearchParams(search).get("review");
    return legacyId
      ? { kind: "redirect", to: reviewPath(legacyId) }
      : { kind: "inbox" };
  }
  const match = /^\/r\/([^/]+)\/?$/.exec(pathname);
  if (match?.[1]) return { kind: "review", id: decodeURIComponent(match[1]) };
  return { kind: "not_found" };
}

const NAVIGATE_EVENT = "code-review:navigate";

export interface NavigationState {
  note?: string;
}

export function navigate(
  to: string,
  { replace = false, state = null }: { replace?: boolean; state?: NavigationState | null } = {},
) {
  if (replace) history.replaceState(state, "", to);
  else history.pushState(state, "", to);
  window.dispatchEvent(new Event(NAVIGATE_EVENT));
}

export function navigationState(): NavigationState {
  const state: unknown = history.state;
  return state !== null && typeof state === "object" ? (state as NavigationState) : {};
}

function subscribe(onChange: () => void) {
  window.addEventListener("popstate", onChange);
  window.addEventListener(NAVIGATE_EVENT, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(NAVIGATE_EVENT, onChange);
  };
}

export function useRoute(): Route {
  const href = useSyncExternalStore(
    subscribe,
    () => location.pathname + location.search,
  );
  const url = new URL(href, location.origin);
  return resolveRoute(url.pathname, url.search);
}

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { eventsUrl } from "./api";
import { readSetting, writeSetting } from "./storage";
import type { DiffStyle, ReviewEvent } from "./types";

const DIFF_STYLE_KEY = "code-review-mcp:diff-style";

export function useDiffStyle(): [DiffStyle, (style: DiffStyle) => void] {
  const [style, setStyle] = useState<DiffStyle>(() =>
    readSetting(DIFF_STYLE_KEY) === "split" ? "split" : "unified",
  );
  const update = useCallback((next: DiffStyle) => {
    setStyle(next);
    writeSetting(DIFF_STYLE_KEY, next);
  }, []);
  return [style, update];
}

/**
 * Subscribe to the review's SSE stream. The first connection is reported as connected,
 * so a page can fetch a snapshot taken after it subscribed. A reconnect is reported as
 * a view_updated.
 */
export function useReviewEvents(reviewId: string, onEvent: (event: ReviewEvent) => void) {
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });
  useEffect(() => {
    const source = new EventSource(eventsUrl(reviewId));
    let connections = 0;
    source.onmessage = (message: MessageEvent<string>) => {
      let event: ReviewEvent;
      try {
        event = JSON.parse(message.data) as ReviewEvent;
      } catch {
        return;
      }
      if (event.type === "connected") {
        connections += 1;
        handler.current(connections > 1 ? { type: "view_updated" } : event);
        return;
      }
      handler.current(event);
    };
    return () => source.close();
  }, [reviewId]);
}

/**
 * Becomes true once the element comes within `rootMargin` of the visible part of
 * `root` (the viewport when null), and stays true.
 */
export function useNearViewport(
  ref: RefObject<Element | null>,
  root: Element | null,
  rootMargin = "800px",
): boolean {
  const [near, setNear] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (near || element === null) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { root, rootMargin },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, root, rootMargin, near]);
  return near;
}

export function useElapsedSeconds(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  return startedAt === null ? 0 : Math.max(0, Math.floor((now - startedAt) / 1000));
}

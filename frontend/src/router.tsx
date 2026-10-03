// A minimal router on the History API: the app has a handful of routes and needs no library.
// Paths carry only identifiers (handoff and trace IDs, filters), never a token.
import { useSyncExternalStore } from "react";
import type { MouseEvent, ReactNode } from "react";

const NAVIGATE = "app:navigate";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange);
  window.addEventListener(NAVIGATE, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(NAVIGATE, onChange);
  };
}

const snapshot = () => window.location.pathname + window.location.search;

export function navigate(to: string): void {
  if (to === snapshot()) return;
  window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAVIGATE));
}

export interface Location {
  path: string;
  query: URLSearchParams;
}

export function useLocation(): Location {
  const current = useSyncExternalStore(subscribe, snapshot);
  const url = new URL(current, window.location.origin);
  return { path: url.pathname, query: url.searchParams };
}

/** A link handled in the page; a click with a modifier still opens a new tab. */
export function Link({
  to,
  className,
  current,
  children,
}: {
  to: string;
  className?: string;
  current?: boolean;
  children: ReactNode;
}) {
  function follow(event: MouseEvent<HTMLAnchorElement>) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  }
  return (
    <a href={to} className={className} onClick={follow} aria-current={current ? "page" : undefined}>
      {children}
    </a>
  );
}

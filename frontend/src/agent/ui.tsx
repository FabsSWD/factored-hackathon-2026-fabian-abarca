// Shared pieces of the agent console: sections, key-value lists, chips, and loading a resource
// with its loading, error and retry states.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { ApiError } from "../api/client";
import { navigate, useLocation } from "../router";

export function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="card section reveal" aria-labelledby={id}>
      <h2 id={id} className="section__title">
        {title}
      </h2>
      {children}
    </section>
  );
}

export function Fields({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([label, value]) => (
        <div className="kv__row" key={label}>
          <dt>{label}</dt>
          <dd>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

type Tone = "copper" | "success" | "danger" | "plain";

export function Chip({ tone = "plain", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`chip chip--${tone}`}>{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted empty">{children}</p>;
}

export function Verified({ value }: { value: boolean }) {
  return <Chip tone={value ? "success" : "danger"}>{value ? "verified" : "not verified"}</Chip>;
}

/** Called when the backend refuses the agent token, so the console asks for it again. */
export const ForbiddenContext = createContext<() => void>(() => {});

interface Result<T> {
  key: string;
  data?: T;
  error?: unknown;
}

export interface Loaded<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Loads a resource whenever `key` changes; a 403 sends the agent back to the sign-in. */
export function useResource<T>(key: string, load: () => Promise<T>): Loaded<T> {
  const forbidden = useContext(ForbiddenContext);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result<T> | null>(null);
  const loader = useRef(load);
  const full = `${key}#${attempt}`;

  useEffect(() => {
    loader.current = load;
  });

  useEffect(() => {
    let current = true;
    loader.current().then(
      (data) => {
        if (current) setResult({ key: full, data });
      },
      (error: unknown) => {
        if (!current) return;
        if (error instanceof ApiError && error.status === 403) forbidden();
        else setResult({ key: full, error });
      },
    );
    return () => {
      current = false;
    };
  }, [full, forbidden]);

  const ready = result?.key === full ? result : null;
  return {
    data: ready?.data,
    error: ready?.error,
    loading: ready === null,
    reload: () => setAttempt((n) => n + 1),
  };
}

/** The loading, not found and failure states of a resource; `children` once it is loaded. */
export function Loading<T>({
  resource,
  missing,
  children,
}: {
  resource: Loaded<T>;
  missing: string;
  children: (data: T) => ReactNode;
}) {
  if (resource.error !== undefined) {
    if (resource.error instanceof ApiError && resource.error.status === 404) {
      return (
        <div className="card reveal" role="alert">
          <p className="error">{missing}</p>
        </div>
      );
    }
    const unavailable =
      resource.error instanceof Error && /^HTTP 50[0-9]$/.test(resource.error.message);
    return (
      <div className="card reveal failure" role="alert">
        <p className="error">
          {unavailable
            ? "The server could not answer (the console may not be configured on it)."
            : "Could not reach the server."}
        </p>
        <button type="button" className="button button--ghost button--small" onClick={resource.reload}>
          Retry
        </button>
      </div>
    );
  }
  if (resource.loading || resource.data === undefined) {
    return (
      <p className="muted loading" role="status">
        Loading
        <span className="typing__cursor" aria-hidden="true" />
      </p>
    );
  }
  return <>{children(resource.data)}</>;
}

export const PAGE_SIZE = 20;

/** The current page (1-based) from the URL's `page`, and the offset it starts at. */
export function usePage(): { page: number; offset: number } {
  const { query } = useLocation();
  const value = Number(query.get("page"));
  const page = Number.isInteger(value) && value > 1 ? value : 1;
  return { page, offset: (page - 1) * PAGE_SIZE };
}

/** The URL of `path` with `values` set in its query (empty values removed); a filter change goes
 * back to the first page. */
export function withQuery(path: string, query: URLSearchParams, values: Record<string, string>): string {
  const next = new URLSearchParams(query);
  for (const [key, value] of Object.entries(values)) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  if (!("page" in values)) next.delete("page");
  const text = next.toString();
  return `${path}${text ? `?${text}` : ""}`;
}

/** A chevron button of the pager: single for one page, double for the first or last page. */
function PagerArrow({
  label,
  disabled,
  onClick,
  double = false,
  back = false,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  double?: boolean;
  back?: boolean;
}) {
  return (
    <button
      type="button"
      className="pager__arrow"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" className={back ? "pager__icon--back" : undefined}>
        {double ? <path d="M3.5 3.5 8 8l-4.5 4.5M8.5 3.5 13 8l-4.5 4.5" /> : <path d="M6 3.5 10.5 8 6 12.5" />}
      </svg>
    </button>
  );
}

/** "21–40 of 134" with the first, previous, next and last pages, and a box to jump to any page;
 * the page lives in the URL. */
export function Pager({ total, offset, count, path }: { total: number; offset: number; count: number; path: string }) {
  const { query } = useLocation();
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (total <= PAGE_SIZE && offset === 0) return null;
  const go = (target: number) => {
    const clamped = Math.min(Math.max(1, target), pages);
    navigate(withQuery(path, query, { page: clamped > 1 ? String(clamped) : "" }));
  };
  const jump = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = Number(new FormData(event.currentTarget).get("page"));
    if (Number.isInteger(value) && value !== page) go(value);
  };
  return (
    <nav className="pager" aria-label="Pages">
      <span className="muted" aria-live="polite">
        {count === 0 ? `0 of ${total}` : `${offset + 1}–${offset + count} of ${total}`}
      </span>
      <span className="pager__buttons">
        <PagerArrow label="First page" disabled={page <= 1} onClick={() => go(1)} double back />
        <PagerArrow label="Previous page" disabled={page <= 1} onClick={() => go(page - 1)} back />
        <form className="pager__page" noValidate onSubmit={jump}>
          <label htmlFor="pager-page">Page</label>
          {/* keyed by page so the box shows the current page again after any navigation */}
          <input
            key={page}
            id="pager-page"
            name="page"
            type="number"
            inputMode="numeric"
            min={1}
            max={pages}
            defaultValue={page}
            className="field__input pager__input"
          />
          <span>{`of ${pages}`}</span>
          <button type="submit" className="button button--ghost button--small">
            Go
          </button>
        </form>
        <PagerArrow label="Next page" disabled={page >= pages} onClick={() => go(page + 1)} />
        <PagerArrow label="Last page" disabled={page >= pages} onClick={() => go(pages)} double />
      </span>
    </nav>
  );
}

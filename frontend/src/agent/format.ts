// How the console writes values. Times are shown in UTC so every agent reads the same clock.

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

export function usd(value: string | number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `USD ${number.toFixed(digits)}` : String(value);
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString("en-US");
}

export const QUEUES = { disputes: "Disputes", fraud: "Fraud", security_review: "Security review" };

export const ACTIONS: Record<string, string> = {
  "ACT-01": "Read customer records",
  "ACT-02": "Create dispute case",
  "ACT-03": "Block card",
  "ACT-04": "Record provisional credit flag",
  "ACT-05": "Transfer to a human",
};

/** Each policy gate (dispute policy §5): its name, what it checks, and what happens when it does not
 * pass. Shown when the agent hovers or focuses a gate in a trace. */
export const GATES: Record<string, { name: string; checks: string; otherwise: string }> = {
  "GATE-01": {
    name: "Supported language",
    checks: "The customer writes in Spanish or Portuguese, unambiguously.",
    otherwise: "One clarification asking for the language, then escalation (ESC-12).",
  },
  "GATE-02": {
    name: "Authenticated session",
    checks: "The identity service issued the session, and it has not expired or gone idle.",
    otherwise: "No account data is read or shown; the customer is asked to authenticate.",
  },
  "GATE-03": {
    name: "Customer status",
    checks: "The customer is Active.",
    otherwise: "Escalation (ESC-08).",
  },
  "GATE-04": {
    name: "Ownership",
    checks: "Every transaction or product referenced belongs to the session customer.",
    otherwise: "Refused without confirming the record exists, and logged as a security event.",
  },
  "GATE-05": {
    name: "Transaction identified",
    checks: "Exactly one of the customer's transactions matches the reference given.",
    otherwise: "Clarification: the candidates are listed, or the customer is asked for a detail.",
  },
  "GATE-06": {
    name: "Transaction status",
    checks: "The transaction is Approved.",
    otherwise: "Inform: pending (wait until posted), declined (nothing charged) or already reversed.",
  },
  "GATE-07": {
    name: "Disputable combination",
    checks: "The transaction type and reason can be handled automatically.",
    otherwise: "Escalation (ESC-14) if a human can handle it; otherwise inform, with a transfer offer.",
  },
  "GATE-08": {
    name: "Filing window",
    checks: "The transaction is at most 60 days old.",
    otherwise: "Up to 120 days: escalation (ESC-07). Older: inform, with a transfer offer.",
  },
  "GATE-09": {
    name: "Product status",
    checks: "The card or account is Active or Blocked.",
    otherwise: "Escalation (ESC-08).",
  },
  "GATE-10": {
    name: "Reason-specific preconditions",
    checks: "The details the dispute reason needs are collected and consistent.",
    otherwise: "Clarification for what is missing, or inform or escalate, depending on the reason.",
  },
  "GATE-11": {
    name: "No duplicate case",
    checks: "No case already exists for this transaction, open or closed.",
    otherwise: "Inform with the existing case and its status, with a transfer offer.",
  },
};

export function action(id: string): string {
  const name = ACTIONS[id];
  return name ? `${id} · ${name}` : id;
}

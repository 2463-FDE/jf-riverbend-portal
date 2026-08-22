"use client";

import { useCallback, useEffect, useState } from "react";
import AgentSummaryPanel from "../components/AgentSummaryPanel";
import { apiFetch, getUser } from "../lib/session";

/**
 * A patient's own results.
 *
 * The content rules this page renders are the client's, settled 2026-08-14,
 * and they are enforced server-side in services/records-service/patient_summary.py —
 * this page must not re-derive any of them. In particular it must never turn a
 * value into a category: if the report did not print the word "normal", the
 * patient does not read it here. Everything shown is either text the report
 * itself contained or a difference between two numbers the report contained.
 *
 * The server sends a `quote` or a `refusal_reason`, never both, so there is no
 * case where this page has to decide what a patient may see.
 */

interface SummaryChange {
  direction: "up" | "down" | "unchanged";
  delta: string;
  unit: string | null;
  from_value: string;
  from_record_id: number;
  from_date: string | null;
}

interface SummaryItem {
  record_id: number;
  title: string | null;
  date: string | null;
  shape: "single_value" | "panel" | "unquotable";
  quote: string | null;
  reference_range: string | null;
  change: SummaryChange | null;
  refusal_reason: string | null;
  source_record_ids: number[];
}

// Neutral wording on purpose. "Higher"/"lower" describes the number and
// nothing else — "better", "worse", or "improving" would be a clinical
// judgment, which is exactly what this feature refuses to make.
const DIRECTION_LABEL: Record<SummaryChange["direction"], string> = {
  up: "Higher than last time",
  down: "Lower than last time",
  unchanged: "Same as last time",
};

function ChangeLine({ change }: { change: SummaryChange }) {
  const amount =
    change.direction === "unchanged"
      ? null
      : `by ${change.delta}${change.unit && change.unit !== "%" ? " " : ""}${change.unit ?? ""}`;

  return (
    <p className="rb-result-change">
      <span className={`rb-result-arrow rb-result-arrow--${change.direction}`} aria-hidden="true">
        {change.direction === "up" ? "↑" : change.direction === "down" ? "↓" : "="}
      </span>
      {DIRECTION_LABEL[change.direction]}
      {amount ? ` ${amount}` : ""}
      {": was "}
      <span className="rb-result-quote">{change.from_value}</span>
      {change.from_date ? ` on ${change.from_date}` : ""}
      {" — "}
      {/* Every figure traces back to the report it came from. */}
      <span className="rb-result-source">source: result #{change.from_record_id}</span>
    </p>
  );
}

function ResultCard({ item }: { item: SummaryItem }) {
  return (
    <li className="rb-result">
      <div className="rb-result-head">
        <h3 className="rb-result-title">{item.title ?? "Result"}</h3>
        {item.date ? <span className="rb-result-date">{item.date}</span> : null}
      </div>

      {item.quote ? (
        <>
          {/* Verbatim. Not reformatted, not rounded, not re-cased. */}
          <p className="rb-result-value">{item.quote}</p>

          {item.reference_range ? (
            <p className="rb-result-range">
              Reference range, as printed on the report:{" "}
              <span className="rb-result-quote">{item.reference_range}</span>
            </p>
          ) : null}

          {item.change ? <ChangeLine change={item.change} /> : null}

          {item.shape === "panel" ? (
            <p className="rb-result-note">
              This result has several measurements, so no single change is shown for it.
            </p>
          ) : null}
        </>
      ) : (
        <p className="rb-result-refusal">{item.refusal_reason}</p>
      )}

      <p className="rb-result-source">
        {item.source_record_ids.length > 1 ? "Sources: " : "Source: "}
        {item.source_record_ids.map((id) => `result #${id}`).join(", ")}
      </p>
    </li>
  );
}

export default function MyResultsPage() {
  const [items, setItems] = useState<SummaryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    // Clear what is on screen BEFORE asking again. Without this, a reload
    // that is denied or fails leaves the previous patient's results rendered
    // underneath the error — clinical values surviving an authorization
    // failure, which is the worst shape this bug could take on a page whose
    // whole job is showing lab results. Every failure path below returns
    // early, so results reappear only when a request actually succeeds.
    setItems(null);
    try {
      const res = await apiFetch("/api/patient/summary");
      if (res.status === 401 || res.status === 403) {
        // Deliberately not "your session expired" — a staff account reaching
        // this page gets the same answer as an unauthenticated one, and
        // neither is told which case applies.
        setError("You are not signed in to a patient account.");
        return;
      }
      if (!res.ok) {
        setError("We could not load your results just now. Please try again shortly.");
        return;
      }
      const body = await res.json();
      setItems(Array.isArray(body.items) ? body.items : []);
    } catch {
      setError("We could not reach the server. Please try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const user = getUser();

  return (
    <main className="rb-results-page">
      <h1>Your results</h1>
      <p className="rb-results-intro">
        These are your results exactly as your care team recorded them. Nothing here is an
        interpretation — if you have questions about what a result means, your care team is the
        right place to ask.
      </p>

      {loading ? <p>Loading your results…</p> : null}

      {error ? (
        <div className="rb-results-error" role="alert">
          <p>{error}</p>
          <button type="button" onClick={() => void load()}>
            Try again
          </button>
        </div>
      ) : null}

      {items && items.length === 0 ? (
        <p className="rb-results-empty">
          There are no results on your record yet. When your care team adds one, it will appear
          here.
        </p>
      ) : null}

      {items && items.length > 0 ? (
        <ul className="rb-results-list">
          {items.map((item) => (
            <ResultCard key={item.record_id} item={item} />
          ))}
        </ul>
      ) : null}

      {/* Separate panel, below the results the report itself printed. The
          deterministic list above is unchanged by this feature. */}
      <AgentSummaryPanel />

      {user ? <p className="rb-results-signed-in">Signed in as {user.username}</p> : null}
    </main>
  );
}

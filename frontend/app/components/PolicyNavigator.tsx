"use client";

import { useState } from "react";
import { apiFetch } from "../lib/session";
import type { PolicyAnswer } from "../lib/types";

/**
 * The policy navigator — a minimal, read-only Q&A over the synthetic policy
 * corpus (w-9-2-planner P3). It cannot book, cancel, change eligibility,
 * approve summaries, release records, send messages, or modify accounts;
 * it only explains how an approved synthetic workflow is supposed to work.
 *
 * Every response carries a truthful provider label (real/fixture/fallback,
 * same vocabulary AgentSummaryPanel uses) and an explicit corpus notice —
 * this is training material, never proof the running application behaves
 * this way, and never real Riverbend policy.
 */

const PROVIDER_TEXT: Record<string, string> = {
  real: "Answered live by the policy navigator (Bedrock).",
  fixture: "Answered from a fixed test example, not a live model call.",
  fallback: "The policy navigator could not reach a live model for this question.",
};

// max_turns is a genuinely different failure from provider unavailability:
// the model was reachable and responding, it just never finished within
// the safe bounded step limit — never describe it as unreachable.
const MAX_TURNS_TEXT = "The policy navigator stopped after reaching its safe step limit.";

const CORPUS_NOTICE =
  "Synthetic training corpus — not real Riverbend policy, and a policy document never proves the running application actually behaves this way.";

const QUESTION_MAX = 500;

export default function PolicyNavigator() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<PolicyAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);

  async function ask() {
    const trimmed = question.trim();
    if (!trimmed) {
      setError("Enter a question first.");
      return;
    }
    setAsking(true);
    setError(null);
    setResult(null);
    try {
      const res = await apiFetch("/api/policy/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      if (res.status === 401) {
        setError("You are not signed in.");
        return;
      }
      if (!res.ok) {
        setError("The policy navigator could not answer that question right now.");
        return;
      }
      setResult((await res.json()) as PolicyAnswer);
    } catch {
      setError("We could not reach the server.");
    } finally {
      setAsking(false);
    }
  }

  const isRefusalOrError =
    result?.termination_reason === "no_evidence" ||
    result?.termination_reason === "provider_error" ||
    result?.termination_reason === "citation_invalid" ||
    result?.termination_reason === "max_turns";

  return (
    <section className="rb-policy-navigator" aria-labelledby="policy-navigator-heading">
      <h2 id="policy-navigator-heading">Policy navigator</h2>
      <p className="rb-policy-navigator__notice">{CORPUS_NOTICE}</p>

      <label htmlFor="policy-question">Ask how an approved workflow is supposed to work</label>
      <textarea
        id="policy-question"
        className="rb-textarea"
        value={question}
        maxLength={QUESTION_MAX}
        onChange={(e) => setQuestion(e.target.value)}
        placeholder="e.g. How does secure messaging authorization work?"
        rows={3}
      />
      <button type="button" className="rb-btn rb-btn--primary" disabled={asking} onClick={() => void ask()}>
        {asking ? "Asking…" : "Ask"}
      </button>

      {error && (
        <p role="alert" className="rb-policy-navigator__error">
          {error}
        </p>
      )}

      {result && (
        <div className="rb-policy-navigator__result" role="status">
          <p className="rb-policy-navigator__label" data-provenance={result.label}>
            {result.termination_reason === "max_turns" ? MAX_TURNS_TEXT : PROVIDER_TEXT[result.label] ?? "Answered."}
          </p>

          <p
            className="rb-policy-navigator__answer"
            data-termination={result.termination_reason}
            role={isRefusalOrError ? "alert" : undefined}
          >
            {result.answer}
          </p>

          {result.citations.length > 0 && (
            <>
              <h3>Where this came from</h3>
              <ul className="rb-policy-navigator__citations">
                {result.citations.map((c) => (
                  <li key={c.citation_id}>
                    {c.title} (version {c.source_version}, {c.section_id})
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </section>
  );
}

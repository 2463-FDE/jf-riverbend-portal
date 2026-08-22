"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../lib/session";

/**
 * The patient's AI-assisted summary — a separate panel, deliberately.
 *
 * The deterministic results list above it is unchanged and stays the primary
 * content: it quotes the report itself and needs no review to be trustworthy.
 * This panel is a different kind of thing (generated, then approved by a
 * clinician) and says so, rather than blending into results the report printed.
 *
 * Nothing here filters by status. The route returns the approved version or
 * `available: false`; pending and rejected text never arrives to be hidden.
 */

interface Citation {
  source_id: string;
  source_version: string;
  citation_id: string;
  category: string | null;
}

interface AgentSummary {
  available: boolean;
  version: number | null;
  provenance_label: string | null;
  generated_text: string | null;
  citations: Citation[];
}

// Plain language, and honest about provenance. A fallback is never described as
// something the assistant wrote — that is the whole reason the label exists.
const PROVENANCE_TEXT: Record<string, string> = {
  real: "Drafted by the AI assistant, then approved by your care team.",
  fixture: "Drafted from a fixed test example, then approved by your care team.",
  fallback:
    "Written directly from your care team's own approved documents — not by the AI assistant — then approved by your care team.",
};

export default function AgentSummaryPanel() {
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setSummary(null);
    try {
      const res = await apiFetch("/api/patient/agent-summary");
      if (res.status === 401 || res.status === 403) {
        setError("You are not signed in to a patient account.");
        return;
      }
      if (!res.ok) {
        setError("We could not load your summary just now.");
        return;
      }
      setSummary((await res.json()) as AgentSummary);
    } catch {
      setError("We could not reach the server.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <section className="rb-agent-summary" aria-labelledby="agent-summary-heading">
        <h2 id="agent-summary-heading">Your summary</h2>
        <p role="alert">{error}</p>
      </section>
    );
  }

  if (!summary || !summary.available) {
    return (
      <section className="rb-agent-summary" aria-labelledby="agent-summary-heading">
        <h2 id="agent-summary-heading">Your summary</h2>
        <p>
          There is no approved summary on your record yet. One appears here only after a
          clinician has reviewed and approved it.
        </p>
      </section>
    );
  }

  const label = summary.provenance_label ?? "";

  return (
    <section className="rb-agent-summary" aria-labelledby="agent-summary-heading">
      <h2 id="agent-summary-heading">Your summary</h2>

      <p className="rb-agent-provenance">
        <span className="rb-agent-label" data-provenance={label}>
          {label}
        </span>{" "}
        {PROVENANCE_TEXT[label] ?? "Approved by your care team."}
      </p>

      <p className="rb-agent-text">{summary.generated_text}</p>

      <p className="rb-agent-version">Version {summary.version}</p>

      {summary.citations.length > 0 && (
        <>
          <h3>Where this came from</h3>
          <ul className="rb-agent-citations">
            {summary.citations.map((c) => (
              <li key={c.citation_id}>
                {c.source_id} (version {c.source_version}
                {c.category ? `, ${c.category}` : ""})
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

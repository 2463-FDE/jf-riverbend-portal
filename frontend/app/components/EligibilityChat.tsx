"use client";

import { useState } from "react";
import { apiFetch } from "../lib/session";
import type { VisitMessageResponse } from "../lib/types";

// Stage 2 (feature-readiness): minimal front-desk chat surface for the
// eligibility assistant. `appointmentId` is sent as the URL's visit_id — the
// gateway verifies the caller is authorized for that appointment's patient
// server-side (services/gateway/visit_authorization.py) before anything
// reaches eligibility-service; this component never sends or receives a
// patient_id/insurance_id itself. Replies are rendered as plain text only
// (never dangerouslySetInnerHTML). No transcript is persisted anywhere —
// this component's local state is the only copy, and it's gone on refresh,
// matching the backend's own "no raw chat persistence" design.
const MAX_MESSAGE_LENGTH = 2000;

type Turn = { role: "user" | "assistant"; text: string };
type Phase = "idle" | "sending" | "unavailable";

export default function EligibilityChat({ appointmentId }: { appointmentId: number }) {
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [deniedReason, setDeniedReason] = useState<string | null>(null);

  async function send() {
    const message = input.trim();
    if (!message || phase === "sending") return;

    setTurns((t) => [...t, { role: "user", text: message }]);
    setInput("");
    setPhase("sending");
    setDeniedReason(null);

    try {
      const res = await apiFetch(`/api/visits/${appointmentId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (res.status === 403) {
        setDeniedReason("You don't have access to discuss this visit.");
        setPhase("unavailable");
        return;
      }
      if (!res.ok) {
        setPhase("unavailable");
        return;
      }
      const data = (await res.json()) as VisitMessageResponse;
      setTurns((t) => [...t, { role: "assistant", text: data.reply || "" }]);
      setPhase("idle");
    } catch {
      setPhase("unavailable");
    }
  }

  if (!open) {
    return (
      <button type="button" className="rb-btn rb-btn--sm" onClick={() => setOpen(true)}>
        Ask about eligibility
      </button>
    );
  }

  return (
    <div className="rb-subsection" style={{ marginTop: 12 }}>
      <div className="rb-list" style={{ marginBottom: 8, maxHeight: 220, overflowY: "auto" }}>
        {turns.length === 0 && (
          <span className="rb-muted">Ask about this visit&apos;s insurance eligibility.</span>
        )}
        {turns.map((turn, i) => (
          <div key={i} className={`rb-alert${turn.role === "assistant" ? "" : " rb-alert--ok"}`} role="status">
            <strong>{turn.role === "user" ? "You: " : "Assistant: "}</strong>
            {turn.text}
          </div>
        ))}
        {phase === "sending" && (
          <span className="rb-muted">
            <span className="rb-spinner" aria-hidden="true" /> Thinking…
          </span>
        )}
        {phase === "unavailable" && (
          <span className="rb-muted">
            {deniedReason || "Couldn't reach the eligibility assistant. Please try again, or check manually."}
          </span>
        )}
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <input
          className="rb-input"
          value={input}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder="e.g. Is this patient's insurance active?"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") send();
          }}
          disabled={phase === "sending"}
        />
        <button
          type="button"
          className="rb-btn rb-btn--primary rb-btn--sm"
          onClick={send}
          disabled={phase === "sending" || !input.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}

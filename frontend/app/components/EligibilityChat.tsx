"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/session";
import type { VisitStreamEvent } from "../lib/types";

// Stage 2 (feature-readiness): minimal front-desk chat surface for the
// eligibility assistant. `appointmentId` is sent as the URL's visit_id — the
// gateway verifies the caller is authorized for that appointment's patient
// server-side (services/gateway/visit_authorization.py) before anything
// reaches eligibility-service; this component never sends or receives a
// patient_id/insurance_id itself. Replies are rendered as plain text only
// (never dangerouslySetInnerHTML). No transcript is persisted anywhere —
// this component's local state is the only copy, and it's gone on refresh,
// matching the backend's own "no raw chat persistence" design.
//
// w-9-2-planner P1b: send() now reads the streaming endpoint
// (/api/visits/[id]/messages/stream) instead of waiting for one complete
// JSON response — each newline-delimited "delta" event is appended to the
// assistant's turn as it arrives. Only "delta" text ever renders; a "done"/
// "error" line's own metadata (tool_called/eligibility_status/etc.) is not
// displayed here — same as before, this UI only ever showed the reply text.
const MAX_MESSAGE_LENGTH = 2000;

type Turn = { role: "user" | "assistant"; text: string };
type Phase = "idle" | "sending" | "unavailable";

export default function EligibilityChat({ appointmentId }: { appointmentId: number }) {
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [deniedReason, setDeniedReason] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Cancellation (w-9-2-planner P1b): if the chat panel closes or the
  // component unmounts mid-stream, stop reading rather than leaving the
  // fetch running for a turn nothing will ever render.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  async function send() {
    const message = input.trim();
    if (!message || phase === "sending") return;

    setTurns((t) => [...t, { role: "user", text: message }]);
    setInput("");
    setPhase("sending");
    setDeniedReason(null);

    const controller = new AbortController();
    abortRef.current = controller;
    let assistantStarted = false;

    function appendDelta(text: string) {
      setTurns((t) => {
        if (!assistantStarted) {
          assistantStarted = true;
          return [...t, { role: "assistant", text }];
        }
        const next = t.slice();
        const last = next[next.length - 1];
        next[next.length - 1] = { ...last, text: last.text + text };
        return next;
      });
    }

    try {
      const res = await apiFetch(`/api/visits/${appointmentId}/messages/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      });
      if (res.status === 403) {
        setDeniedReason("You don't have access to discuss this visit.");
        setPhase("unavailable");
        return;
      }
      if (!res.ok || !res.body) {
        setPhase("unavailable");
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminal: "done" | "error" | null = null;
      let errorText: string | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newlineAt;
        while ((newlineAt = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newlineAt);
          buffer = buffer.slice(newlineAt + 1);
          if (!line) continue;
          let event: VisitStreamEvent;
          try {
            event = JSON.parse(line);
          } catch {
            continue; // a malformed line must never crash the chat
          }
          if (event.kind === "delta" && event.text) {
            appendDelta(event.text);
          } else if (event.kind === "done") {
            terminal = "done";
          } else if (event.kind === "error") {
            terminal = "error";
            errorText = event.text ?? null;
          }
        }
      }

      if (terminal === "error") {
        // Never represent a partial answer as complete — even if some
        // delta text already streamed, an error terminal means the turn
        // did not finish successfully.
        setDeniedReason(errorText);
        setPhase("unavailable");
      } else if (terminal === null) {
        // The connection ended with no terminal event at all — a
        // truncated stream, not a completed one.
        setPhase("unavailable");
      } else {
        setPhase("idle");
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
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

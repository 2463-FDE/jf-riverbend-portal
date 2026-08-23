"use client";

import { useCallback, useEffect, useState } from "react";
import Card from "../components/Card";
import StatusBadge from "../components/StatusBadge";
import { IconMessages } from "../components/icons";
import { apiFetch, getUser } from "../lib/session";
import type { PortalUser, ThreadDetail, ThreadSummary } from "../lib/types";
import { fmtDateTime } from "../lib/format";

/**
 * Secure patient-clinician messaging (W9.2).
 *
 * One page serves both audiences, because the backend already does: `/api/
 * threads` is scoped to the caller's own grants (a patient's own thread, or
 * every patient a clinician currently holds), so there is nothing left for
 * this page to filter by role except which ACTIONS it offers — starting a
 * new thread is patient-only, closing/reopening one is staff-only. Reading
 * and replying are identical either way.
 *
 * sessionStorage is only readable client-side, so `user` starts undefined on
 * both the server render and the first client render (same output, no
 * hydration mismatch) — see app/page.tsx for the same pattern and why.
 */
export default function MessagesPage() {
  const [user, setUser] = useState<PortalUser | null | undefined>(undefined);
  useEffect(() => {
    setUser(getUser());
  }, []);

  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [listError, setListError] = useState(false);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [threadBusy, setThreadBusy] = useState(false);
  const [threadError, setThreadError] = useState<string | null>(null);

  const [replyBody, setReplyBody] = useState("");
  const [replying, setReplying] = useState(false);

  const [composeOpen, setComposeOpen] = useState(false);
  const [composeSubject, setComposeSubject] = useState("");
  const [composeBody, setComposeBody] = useState("");
  const [composing, setComposing] = useState(false);
  const [composeError, setComposeError] = useState<string | null>(null);

  const [statusBusy, setStatusBusy] = useState(false);

  const loadThreads = useCallback(async () => {
    setListError(false);
    try {
      const res = await apiFetch("/api/threads");
      if (!res.ok) {
        setListError(true);
        return;
      }
      const body = await res.json();
      setThreads(Array.isArray(body.items) ? body.items : []);
    } catch {
      setListError(true);
    }
  }, []);

  useEffect(() => {
    if (user === undefined) return;
    void loadThreads();
  }, [user, loadThreads]);

  const openThread = useCallback(
    async (id: number) => {
      setSelectedId(id);
      setThread(null);
      setThreadError(null);
      setThreadBusy(true);
      try {
        const res = await apiFetch(`/api/threads/${id}`);
        if (!res.ok) {
          setThreadError(
            res.status === 404
              ? "This conversation is not available."
              : "We could not load this conversation just now."
          );
          return;
        }
        setThread(await res.json());
        void loadThreads(); // this thread's unread count just changed
      } catch {
        setThreadError("We could not reach the server.");
      } finally {
        setThreadBusy(false);
      }
    },
    [loadThreads]
  );

  async function sendReply() {
    const body = replyBody.trim();
    if (!selectedId || !body) return;
    setReplying(true);
    setThreadError(null);
    try {
      const res = await apiFetch(`/api/threads/${selectedId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body, idempotency_key: crypto.randomUUID() }),
      });
      if (!res.ok) {
        setThreadError(
          res.status === 409 ? "This thread is closed." : "Could not send that message. Please try again."
        );
        return;
      }
      setReplyBody("");
      await openThread(selectedId);
    } catch {
      setThreadError("We could not reach the server.");
    } finally {
      setReplying(false);
    }
  }

  async function submitCompose() {
    const subject = composeSubject.trim();
    const body = composeBody.trim();
    if (!subject || !body) return;
    setComposing(true);
    setComposeError(null);
    try {
      const res = await apiFetch("/api/patient/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subject, body, idempotency_key: crypto.randomUUID() }),
      });
      if (!res.ok) {
        setComposeError("Could not start a new message just now. Please try again.");
        return;
      }
      const created = await res.json();
      setComposeOpen(false);
      setComposeSubject("");
      setComposeBody("");
      await loadThreads();
      await openThread(created.id);
    } catch {
      setComposeError("We could not reach the server.");
    } finally {
      setComposing(false);
    }
  }

  async function toggleStatus() {
    if (!thread) return;
    const next = thread.status === "open" ? "closed" : "open";
    setStatusBusy(true);
    setThreadError(null);
    try {
      const res = await apiFetch(`/api/threads/${thread.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      if (!res.ok) {
        setThreadError("Could not update this thread. Please try again.");
        return;
      }
      setThread(await res.json());
      void loadThreads();
    } catch {
      setThreadError("We could not reach the server.");
    } finally {
      setStatusBusy(false);
    }
  }

  if (user === undefined) return null;
  const isPatient = user?.role === "patient";

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Messages</h1>
        <p>Portal messaging is not for emergencies. For urgent medical concerns, call 911.</p>
      </div>

      {isPatient && (
        <Card>
          {!composeOpen ? (
            <button type="button" className="rb-btn rb-btn--primary" onClick={() => setComposeOpen(true)}>
              New message to your care team
            </button>
          ) : (
            <div className="rb-field" style={{ marginBottom: 0 }}>
              <label className="rb-field__label" htmlFor="compose-subject">
                Subject
              </label>
              <input
                id="compose-subject"
                className="rb-input"
                value={composeSubject}
                onChange={(e) => setComposeSubject(e.target.value)}
                style={{ marginBottom: 10 }}
              />
              <label className="rb-field__label" htmlFor="compose-body">
                Message
              </label>
              <textarea
                id="compose-body"
                className="rb-textarea"
                value={composeBody}
                onChange={(e) => setComposeBody(e.target.value)}
              />
              {composeError && (
                <p role="alert" className="rb-muted" style={{ marginTop: 8 }}>
                  {composeError}
                </p>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <button
                  type="button"
                  className="rb-btn rb-btn--primary"
                  disabled={composing || !composeSubject.trim() || !composeBody.trim()}
                  onClick={() => void submitCompose()}
                >
                  {composing ? "Sending…" : "Send"}
                </button>
                <button
                  type="button"
                  className="rb-btn"
                  disabled={composing}
                  onClick={() => {
                    setComposeOpen(false);
                    setComposeError(null);
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </Card>
      )}

      <div className="rb-grid" style={{ gridTemplateColumns: "320px 1fr", alignItems: "start" }}>
        <Card title="Inbox" icon={<IconMessages />}>
          {listError ? (
            <p role="alert" className="rb-muted">
              We could not load your messages just now.
            </p>
          ) : threads === null ? (
            <Loading label="Loading messages…" />
          ) : threads.length === 0 ? (
            <div className="rb-empty">No messages yet.</div>
          ) : (
            <div className="rb-list">
              {threads.map((t) => {
                const active = t.id === selectedId;
                return (
                  <button
                    key={t.id}
                    type="button"
                    className="rb-listrow rb-listrow--clickable"
                    aria-pressed={active}
                    style={active ? { borderColor: "var(--rb-primary)" } : undefined}
                    onClick={() => void openThread(t.id)}
                  >
                    <div className="rb-listrow__main">
                      <div className="rb-listrow__title">
                        {t.subject}
                        {t.unread_count > 0 && (
                          <span className="rb-badge rb-badge--info" style={{ marginLeft: 8 }}>
                            {t.unread_count} new
                          </span>
                        )}
                      </div>
                      <div className="rb-listrow__meta">
                        {!isPatient && t.patient_name && <span>{t.patient_name}</span>}
                        {t.last_sender_name && <span>{t.last_sender_name}</span>}
                        {t.last_message_at && <span>{fmtDateTime(t.last_message_at)}</span>}
                        <StatusBadge status={t.status === "open" ? "active" : "cancelled"} label={t.status} />
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </Card>

        <Card title={thread?.subject ?? "Select a conversation"}>
          {threadError && (
            <p role="alert" className="rb-muted">
              {threadError}
            </p>
          )}
          {selectedId && threadBusy && !thread ? <Loading label="Loading conversation…" /> : null}

          {thread && (
            <>
              {!isPatient && (
                <p className="rb-muted" style={{ marginTop: 0 }}>
                  {thread.patient_name} · <StatusBadge status={thread.status === "open" ? "active" : "cancelled"} label={thread.status} />
                </p>
              )}

              <div className="rb-list">
                {thread.messages.map((m) => (
                  <div key={m.id} className="rb-listrow" style={{ flexDirection: "column", alignItems: "stretch" }}>
                    <div className="rb-listrow__meta">
                      <strong>{m.sender_name}</strong>
                      <span>{fmtDateTime(m.created_at)}</span>
                    </div>
                    <p style={{ margin: "6px 0 0" }}>{m.body}</p>
                  </div>
                ))}
              </div>

              {thread.status === "closed" && (
                <p className="rb-muted" style={{ marginTop: 10 }}>
                  This thread is closed.{!isPatient ? " Reopen it to reply." : ""}
                </p>
              )}

              <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                <input
                  className="rb-input"
                  placeholder="Write a reply"
                  value={replyBody}
                  onChange={(e) => setReplyBody(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void sendReply()}
                  disabled={thread.status === "closed"}
                />
                <button
                  type="button"
                  className="rb-btn rb-btn--primary"
                  disabled={replying || thread.status === "closed" || !replyBody.trim()}
                  onClick={() => void sendReply()}
                >
                  {replying ? "Sending…" : "Send"}
                </button>
              </div>

              {!isPatient && (
                <button
                  type="button"
                  className="rb-btn"
                  style={{ marginTop: 10 }}
                  disabled={statusBusy}
                  onClick={() => void toggleStatus()}
                >
                  {statusBusy ? "Updating…" : thread.status === "open" ? "Close thread" : "Reopen thread"}
                </button>
              )}
            </>
          )}

          {!selectedId && !threadError && (
            <div className="rb-empty">Choose a conversation from the inbox.</div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="rb-loading">
      <span className="rb-spinner rb-spinner--dark" aria-hidden="true" /> {label}
    </div>
  );
}

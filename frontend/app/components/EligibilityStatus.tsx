"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/session";
import { fmtDateTime } from "../lib/format";
import type { EligibilityJobResponse } from "../lib/types";

// Stage 3: minimal eligibility status surface for the async job kicked off
// by /intake (eligibility_job_id). Polling is bounded — it never spins
// forever — and every branch below is deliberately distinct: an unknown,
// failed, or stale result is NEVER rendered as inactive/current. See
// services/eligibility-service/jobs.py for the underlying state machine.
const POLL_INTERVAL_MS = 3000;
const MAX_POLLS = 20; // ~60s of automatic polling before we stop and wait for a manual refresh

type Phase = "polling" | "settled" | "stopped" | "error";

export default function EligibilityStatus({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<EligibilityJobResponse | null>(null);
  const [phase, setPhase] = useState<Phase>("polling");
  const [pollCount, setPollCount] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function fetchOnce() {
    try {
      const res = await apiFetch(`/api/eligibility/jobs/${encodeURIComponent(jobId)}`);
      if (!res.ok) {
        setPhase("error");
        return;
      }
      const data = (await res.json()) as EligibilityJobResponse;
      setJob(data);
      if (data.status === "succeeded" || data.status === "dead_letter" || data.status === "failed") {
        setPhase("settled");
      }
    } catch {
      setPhase("error");
    }
  }

  useEffect(() => {
    fetchOnce();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  useEffect(() => {
    if (phase !== "polling") return;
    if (pollCount >= MAX_POLLS) {
      setPhase("stopped"); // bounded: stop auto-polling rather than spin forever
      return;
    }
    timerRef.current = setTimeout(async () => {
      await fetchOnce();
      setPollCount((n) => n + 1);
    }, POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, pollCount]);

  async function manualRefresh() {
    setPollCount(0);
    setPhase("polling");
    await fetchOnce();
  }

  async function retry() {
    setRetrying(true);
    try {
      await apiFetch(`/api/eligibility/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
      setPollCount(0);
      setPhase("polling");
      await fetchOnce();
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div className="rb-alert" role="status" style={{ marginTop: 12 }}>
      {render()}
    </div>
  );

  function render() {
    if (phase === "error") {
      // A failure to even reach our own status endpoint — unknown, never
      // "inactive".
      return (
        <span>
          Could not check eligibility status right now.{" "}
          <button type="button" className="rb-btn" onClick={manualRefresh}>
            Try again
          </button>
        </span>
      );
    }

    if (!job) {
      return <span>Checking insurance eligibility…</span>;
    }

    if (job.status === "queued" || job.status === "running" || job.status === "retryable") {
      if (phase === "stopped") {
        return (
          <span>
            Eligibility verification is still in progress. This is taking longer than usual — check back
            shortly, or{" "}
            <button type="button" className="rb-btn" onClick={manualRefresh}>
              refresh now
            </button>
            .
          </span>
        );
      }
      return <span><span className="rb-spinner" aria-hidden="true" /> Checking insurance eligibility…</span>;
    }

    if (job.status === "failed" || job.status === "dead_letter") {
      const canRetry = job.manual_retry_count < job.max_manual_retries;
      return (
        <span>
          We couldn&apos;t verify insurance eligibility.{" "}
          {canRetry ? (
            <button type="button" className="rb-btn" onClick={retry} disabled={retrying}>
              {retrying ? "Retrying…" : "Retry"}
            </button>
          ) : (
            "Please check eligibility manually."
          )}
        </span>
      );
    }

    // status === "succeeded" — branch on the actual coverage result. Stale
    // and unknown are NEVER shown as inactive/current.
    switch (job.result_status) {
      case "active":
        return <span>Insurance eligibility: <strong>active</strong>.</span>;
      case "inactive":
        return <span>Insurance eligibility: <strong>inactive</strong>.</span>;
      case "stale":
        return (
          <span>
            Showing the last known eligibility result (checked {fmtDateTime(job.result_checked_at)}) — it
            could not be re-verified just now and may be outdated.
          </span>
        );
      case "unknown":
      default:
        return <span>Insurance eligibility could not be verified. Please check manually.</span>;
    }
  }
}

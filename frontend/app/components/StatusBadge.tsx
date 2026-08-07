import type { ReactNode } from "react";

type Variant = "ok" | "warn" | "bad" | "info" | "neutral";

const MAP: Record<string, Variant> = {
  // appointment / generic
  confirmed: "ok",
  booked: "ok",
  scheduled: "ok",
  completed: "ok",
  available: "ok",
  open: "ok",
  active: "ok",
  fulfilled: "ok",
  normal: "ok",
  pending: "warn",
  requested: "warn",
  "in-progress": "warn",
  in_progress: "warn",
  waitlist: "warn",
  held: "warn",
  escalated: "warn",
  cancelled: "bad",
  canceled: "bad",
  denied: "bad",
  abnormal: "bad",
  high: "bad",
  low: "bad",
  critical: "bad",
  expired: "bad",
  refused: "bad",
  booking: "info",
};

export function statusVariant(status: string | undefined): Variant {
  if (!status) return "neutral";
  return MAP[status.toLowerCase().trim()] ?? "neutral";
}

export default function StatusBadge({
  status,
  label,
}: {
  status: string | undefined;
  label?: ReactNode;
}) {
  const variant = statusVariant(status);
  const text = label ?? status ?? "—";
  return (
    <span
      className={`rb-badge rb-badge--${variant}`}
      role="status"
      aria-label={`Status: ${status ?? "unknown"}`}
    >
      {text}
    </span>
  );
}

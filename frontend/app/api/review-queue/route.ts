import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Cases waiting on a clinician. Gated upstream on summary_review.decide plus
// records.read — held by clinician and nursing_ma only, not by the deprecated
// `staff` role and not by `lab`. The queue is additionally scoped to patients
// the caller holds a grant for.
export async function GET(req: NextRequest) {
  const limit = req.nextUrl.searchParams.get("limit") ?? "50";
  return proxy(req, `/review-queue?limit=${encodeURIComponent(limit)}`);
}

import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Cases waiting on a clinician. Staff-only upstream: the gateway gates this on
// records.write, which the patient role does not hold.
export async function GET(req: NextRequest) {
  const limit = req.nextUrl.searchParams.get("limit") ?? "50";
  return proxy(req, `/review-queue?limit=${encodeURIComponent(limit)}`);
}

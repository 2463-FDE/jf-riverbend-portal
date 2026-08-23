import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// The inbox — shared by both audiences. Gated upstream on messages.read, held
// by both `patient` and `clinician`/`nursing_ma`; the result set is already
// scoped to the caller's own grants (a patient's own thread(s), or every
// patient a clinician currently holds), so there is nothing here for this
// route to filter further.
export async function GET(req: NextRequest) {
  const limit = req.nextUrl.searchParams.get("limit") ?? "50";
  return proxy(req, `/threads?limit=${encodeURIComponent(limit)}`);
}

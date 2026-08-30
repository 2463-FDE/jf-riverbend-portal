import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  // W10 Final 2 Stage 1: this used to hardcode body: {}, discarding whatever
  // the caller sent — roi-service's FulfillRequest.authorization_id is
  // required, so every fulfillment through this route always 422'd, and the
  // gateway (before this stage) flattened that into a false 200. Forward the
  // real body — the caller (roi/page.tsx) now sends the staff-selected
  // authorization_id.
  const body = await req.json();
  return proxy(req, `/roi/requests/${encodeURIComponent(id)}/fulfill`, {
    method: "POST",
    body,
  });
}

import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Approve or reject. This is the call that changes what a patient can see, so
// it forwards the decision and nothing else — the deciding clinician is
// identified by the gateway from the session, never from anything this route
// could be persuaded to send.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  return proxy(req, `/review-queue/${id}/decision`, { method: "POST", body });
}

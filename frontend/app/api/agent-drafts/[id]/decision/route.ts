import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Approve or reject one draft version. Forwards the decision and nothing else:
// the deciding clinician is identified by the gateway from the session.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  return proxy(req, `/agent-drafts/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    body,
  });
}

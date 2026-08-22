import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// The clinician-only draft: GET reads the latest version whatever its status,
// POST generates a new one. This is the ONE browser route that can return
// unapproved draft text; the gateway gates both on summary_review.decide plus a
// grant for the patient. The patient's own route is a different file.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${encodeURIComponent(id)}/agent-draft`);
}

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${encodeURIComponent(id)}/agent-draft`, {
    method: "POST",
    body: {},
  });
}

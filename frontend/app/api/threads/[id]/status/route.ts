import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Close/reopen. records-service enforces staff-only on top of the grant
// check — a patient calling this gets a 403, not a silently ignored no-op.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  return proxy(req, `/threads/${encodeURIComponent(id)}/status`, { method: "POST", body });
}

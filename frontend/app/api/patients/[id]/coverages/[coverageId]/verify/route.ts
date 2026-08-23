import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Request a verification. The gateway derives the member id server-side and
// never accepts one from the browser — this route sends no body at all.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string; coverageId: string }> }
) {
  const { id, coverageId } = await params;
  return proxy(
    req,
    `/patients/${encodeURIComponent(id)}/coverages/${encodeURIComponent(coverageId)}/verify`,
    { method: "POST", body: {} }
  );
}

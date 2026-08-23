import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Gated upstream on billing.read plus an active grant for this patient.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${encodeURIComponent(id)}/coverages`);
}

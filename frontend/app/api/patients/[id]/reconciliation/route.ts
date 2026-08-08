import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Stage 2 (Week 6): read-only "possible duplicate patient" reconciliation
// view. Same StaffAccessGate boundary as /patients/[id]/view (authenticated-
// staff, NOT patient-specific — does not fix RIV-201). No `purpose` param.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${encodeURIComponent(id)}/reconciliation`);
}

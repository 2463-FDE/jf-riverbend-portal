import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Stage 3: bounded, evidence-cited patient-view agent. Gated by the gateway's
// StaffAccessGate check (authenticated-staff, NOT patient-specific — see
// services/gateway/app.py's proxy_patient_view comment). This is additive;
// it does not change /api/records or /api/patients/[id] above, which remain
// the documented IDOR (RIV-201).
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const purpose = req.nextUrl.searchParams.get("purpose");
  const qs = purpose ? `?purpose=${encodeURIComponent(purpose)}` : "";
  return proxy(req, `/patients/${encodeURIComponent(id)}/view${qs}`);
}

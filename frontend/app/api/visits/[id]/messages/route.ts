import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Stage 2 (feature-readiness): `id` is a real appointments.id — the gateway
// verifies the caller is authorized for that appointment's patient before
// proxying anything downstream (services/gateway/visit_authorization.py).
// Only `message` is ever sent; patient_id/insurance_id are derived
// server-side and dropped from any request body here.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  return proxy(req, `/visits/${encodeURIComponent(id)}/messages`, {
    method: "POST",
    body: { message: body?.message },
  });
}

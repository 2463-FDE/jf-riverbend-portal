import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Front desk issues a portal invitation while registering the patient. The
// gateway returns the code exactly once and never stores it — so this route
// forwards the response through untouched and deliberately does not log it.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${id}/invitation`, { method: "POST", body: {} });
}

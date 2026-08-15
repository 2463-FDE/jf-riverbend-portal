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

// Revoke an outstanding invitation before anyone redeems it.
//
// Needed for the case the issue route's 409 points at, and for the ordinary
// desk mistake: a code read aloud to the wrong person, or issued against the
// wrong patient. Only unredeemed invitations are affected upstream — revoking
// an invitation never disables an account a patient is already using.
export async function DELETE(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/patients/${id}/invitation`, { method: "DELETE" });
}

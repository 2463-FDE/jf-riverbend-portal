import { NextRequest, NextResponse } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/app/lib/gateway";

// Name-only projection of GET /patients/{id}, for the staff screens that
// identify a patient by a bare integer and need a name beside it.
//
// Deliberately NOT `proxy(req, "/patients/{id}")`: that helper relays the
// upstream body verbatim, and PatientDetail carries `ssn` and `dob`.
// records-service's _redact_clinical_fields withholds `notes` only, so any
// holder of patients.read gets the SSN — reusing the existing route would ship
// a patient's SSN to the browser on two staff screens in order to render a
// name. We read the upstream response server-side and return {id, name} only;
// nothing else crosses to the client.
//
// Status is relayed as-is. 403 is a NORMAL outcome here, not an error: grants
// are per-(actor, patient), so a staff user routinely lacks one for the id
// they typed. The caller renders a fallback for it.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await fetch(
      `${gatewayUrl()}/patients/${encodeURIComponent(id)}`,
      { headers: gatewayHeaders(req, false), cache: "no-store" }
    );
    if (!res.ok) {
      return NextResponse.json({ error: "name unavailable" }, { status: res.status });
    }
    const patient = await res.json();
    return NextResponse.json({ id: patient?.id ?? Number(id), name: patient?.name ?? null });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "gateway unreachable" },
      { status: 502 }
    );
  }
}

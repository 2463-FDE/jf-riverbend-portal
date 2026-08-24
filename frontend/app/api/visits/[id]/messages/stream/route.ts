import { NextRequest } from "next/server";
import { gatewayHeaders, gatewayUrl } from "@/app/lib/gateway";

// w-9-2-planner P1b: streaming counterpart to ../route.ts. Unlike that
// route (which uses lib/gateway.ts's `proxy()` helper — buffers the whole
// response body before returning), this relays the gateway's
// newline-delimited-JSON stream straight through as it arrives, never
// buffering it here. `id`/auth/body-shaping rules are identical to the
// non-streaming route: only `message` is ever sent; patient_id/
// insurance_id/coverage_on_file are derived server-side at the gateway and
// dropped from anything sent here.
const UNAVAILABLE_LINE =
  JSON.stringify({
    kind: "error",
    text: "The eligibility assistant isn't available right now. Please check eligibility manually.",
    tool_called: null,
    eligibility_status: null,
    termination_reason: "provider_error",
    turns_used: null,
  }) + "\n";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();

  try {
    const upstream = await fetch(
      `${gatewayUrl()}/visits/${encodeURIComponent(id)}/messages/stream`,
      {
        method: "POST",
        headers: gatewayHeaders(req, true),
        body: JSON.stringify({ message: body?.message }),
        cache: "no-store",
      }
    );
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") || "application/x-ndjson" },
    });
  } catch {
    // The gateway itself was unreachable — this hop has no stream to relay
    // at all. Same "one sanitized terminal event, never a raw error" rule
    // the backend already applies to a downstream failure, applied here to
    // the one additional hop this route adds.
    return new Response(UNAVAILABLE_LINE, {
      status: 200,
      headers: { "Content-Type": "application/x-ndjson" },
    });
  }
}

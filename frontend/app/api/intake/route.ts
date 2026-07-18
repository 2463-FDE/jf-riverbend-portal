import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function POST(req: NextRequest) {
  const body = await req.json();

  // Stage 3 (RIV-088/RIV-141 fix): intake-service no longer verifies
  // eligibility inline — it enqueues an async job and returns promptly. The
  // artificial ~4-5s frontend delay that used to mirror the old blocking
  // backend call has been removed; /intake responds as fast as the gateway
  // does.
  return proxy(req, "/intake", { method: "POST", body });
}

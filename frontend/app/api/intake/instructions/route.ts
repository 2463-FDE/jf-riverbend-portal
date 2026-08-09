import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Stage 1 (feature-readiness): plain-language explanation of one intake
// wizard step. The only body field is `step`; no patient data is ever sent.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/intake/instructions", { method: "POST", body });
}

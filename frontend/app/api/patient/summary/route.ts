import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// A patient reading their own results.
//
// Note there is no patient id in this path, here or upstream: the gateway
// derives the chart from the signed-in account's own users.patient_id. There
// is deliberately no way for the browser to ask for someone else's results,
// so this route has nothing to validate — it forwards the bearer token and
// the gateway decides.
export async function GET(req: NextRequest) {
  return proxy(req, "/patient/me/summary");
}

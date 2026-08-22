import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// A patient reading their own APPROVED summary. As with /patient/me/summary
// there is no patient id in this path: the gateway derives the chart from the
// signed-in account, and upstream returns the approved version or nothing at
// all — pending and rejected text never reaches this route to be filtered.
export async function GET(req: NextRequest) {
  return proxy(req, "/patient/me/agent-summary");
}

import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// The patient asks for a summary of their own chart. No patient id in the path,
// here or upstream: the gateway derives it from the signed-in account. The
// response is a receipt — version, status, label, citations, correlation id —
// and never draft text, which stays behind the clinician gate until approved.
export async function POST(req: NextRequest) {
  return proxy(req, "/patient/me/agent-summary/request", { method: "POST", body: {} });
}

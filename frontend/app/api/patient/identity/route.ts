import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// A patient reading their own name and id. No patient id in this path, as
// with /patient/me/summary: the gateway derives the chart from the signed-in
// account, so there is nothing here for the browser to substitute.
export async function GET(req: NextRequest) {
  return proxy(req, "/patient/me/identity");
}

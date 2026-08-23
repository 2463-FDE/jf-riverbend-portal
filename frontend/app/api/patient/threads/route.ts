import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// A patient starts a new thread. No patient id in this path, same as
// /api/patient/identity and /api/patient/summary — the gateway resolves the
// chart from the signed-in account, never from anything the browser sends.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/patient/me/threads", { method: "POST", body });
}

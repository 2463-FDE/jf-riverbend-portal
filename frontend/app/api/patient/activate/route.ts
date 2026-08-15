import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Public: the person activating has no account yet, so no bearer token is
// forwarded. The gateway returns one generic answer for every failure on
// purpose — do not try to enrich it here, or this becomes a way to discover
// which codes exist.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/patient/activate", { method: "POST", body });
}

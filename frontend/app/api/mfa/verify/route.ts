import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Public — no Authorization involved at all. `challenge_token` in the body
// is the only credential this route ever needs (the login-challenge
// screen, for an already-enrolled account).
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/mfa/verify", { method: "POST", body });
}

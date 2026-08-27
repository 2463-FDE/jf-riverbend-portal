import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Public — proxy() forwards Authorization only when the caller has one
// (voluntary enrollment, already signed in). A forced first-time
// enrollment has no session yet and authenticates via `challenge_token` in
// the body instead — the gateway route accepts either.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/mfa/enroll/start", { method: "POST", body });
}

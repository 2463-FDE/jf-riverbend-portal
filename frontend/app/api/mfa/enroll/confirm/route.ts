import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Same dual-auth shape as /mfa/enroll/start — see that route's comment.
export async function POST(req: NextRequest) {
  const body = await req.json();
  return proxy(req, "/mfa/enroll/confirm", { method: "POST", body });
}

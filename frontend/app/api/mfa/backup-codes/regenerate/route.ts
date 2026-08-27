import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Self-service — requires an existing session (proxy() forwards it).
export async function POST(req: NextRequest) {
  return proxy(req, "/mfa/backup-codes/regenerate", { method: "POST", body: {} });
}

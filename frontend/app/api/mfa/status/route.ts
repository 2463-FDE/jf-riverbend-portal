import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function GET(req: NextRequest) {
  return proxy(req, "/mfa/status", { json: false });
}

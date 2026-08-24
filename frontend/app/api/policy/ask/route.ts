import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// w-9-2-planner P3: any authenticated session may ask — the gateway derives
// role server-side and forwards only X-Actor-Id, never a client-supplied
// role. Only `question` is ever taken from the request body.
export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  return proxy(req, "/policy/ask", { method: "POST", body: { question: body.question } });
}

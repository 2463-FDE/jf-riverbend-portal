import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// Reply to an existing, already-authorized thread. Gated upstream on
// messages.write, held by both audiences; a closed thread refuses with 409
// regardless of who is replying.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  return proxy(req, `/threads/${encodeURIComponent(id)}/messages`, { method: "POST", body });
}

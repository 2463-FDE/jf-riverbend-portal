import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import EligibilityChat from "./EligibilityChat";

// w-9-2-planner P1b: send() reads the streaming endpoint's
// newline-delimited JSON body incrementally. These tests focus on exactly
// what the skill's own essential-test list asks for here: final-token
// rendering, completion/error handling, and cancellation — the
// authorization/stored-vs-verified/leakage boundaries themselves are
// already covered by the backend's own extensive test suites; this
// component never sees anything but "delta"/"done"/"error" lines regardless.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "../lib/session";

function streamResponse(lines: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const line of lines) {
        controller.enqueue(encoder.encode(line + "\n"));
      }
      controller.close();
    },
  });
  return { ok: status >= 200 && status < 300, status, body } as unknown as Response;
}

function line(event: Record<string, unknown>): string {
  return JSON.stringify(event);
}

async function open() {
  fireEvent.click(screen.getByRole("button", { name: /ask about eligibility/i }));
}

async function sendMessage(text: string) {
  const input = screen.getByPlaceholderText(/is this patient's insurance active/i);
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
}

describe("EligibilityChat streaming", () => {
  it("renders delta text incrementally into one assistant turn, ending idle", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      streamResponse([
        line({ kind: "delta", text: "Cove" }),
        line({ kind: "delta", text: "rage is active." }),
        line({ kind: "done", tool_called: true, termination_reason: "answered", turns_used: 1 }),
      ])
    );
    render(<EligibilityChat appointmentId={7} />);
    await open();

    await sendMessage("am I covered?");

    await waitFor(() => expect(screen.getByText("Coverage is active.")).toBeInTheDocument());
    // Exactly one assistant turn, not one per delta chunk.
    expect(screen.getAllByText(/^Assistant:$/)).toHaveLength(1);
    expect(screen.getByPlaceholderText(/is this patient's insurance active/i)).not.toBeDisabled();
  });

  it("shows the sanitized error text and never treats a partial answer as complete", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      streamResponse([
        line({ kind: "delta", text: "Here's what I found so far..." }),
        line({ kind: "error", text: "Verification unavailable right now.", termination_reason: "provider_error" }),
      ])
    );
    render(<EligibilityChat appointmentId={7} />);
    await open();

    await sendMessage("check now");

    await waitFor(() => expect(screen.getByText("Verification unavailable right now.")).toBeInTheDocument());
    // The partial text is still visible (it's real model output), but the
    // turn must not be presented as a normal completed answer — the
    // "unavailable" note appears alongside it.
    expect(screen.getByText(/here's what i found so far/i)).toBeInTheDocument();
  });

  it("degrades to unavailable if the stream ends with no terminal event at all", async () => {
    vi.mocked(apiFetch).mockResolvedValue(streamResponse([line({ kind: "delta", text: "partial" })]));
    render(<EligibilityChat appointmentId={7} />);
    await open();

    await sendMessage("check now");

    await waitFor(() =>
      expect(screen.getByText(/couldn't reach the eligibility assistant/i)).toBeInTheDocument()
    );
  });

  it("shows a denial message on 403 without attempting to read a body", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false, status: 403 } as Response);
    render(<EligibilityChat appointmentId={7} />);
    await open();

    await sendMessage("am I covered?");

    await waitFor(() =>
      expect(screen.getByText(/don't have access to discuss this visit/i)).toBeInTheDocument()
    );
  });

  it("ignores a malformed line instead of crashing the chat", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      streamResponse(["not valid json{{{", line({ kind: "delta", text: "Active." }), line({ kind: "done" })])
    );
    render(<EligibilityChat appointmentId={7} />);
    await open();

    await sendMessage("check now");

    await waitFor(() => expect(screen.getByText("Active.")).toBeInTheDocument());
  });

  it("aborts the in-flight stream request on unmount", async () => {
    let capturedSignal: AbortSignal | undefined;
    vi.mocked(apiFetch).mockImplementation((_url, init) => {
      capturedSignal = (init as RequestInit)?.signal ?? undefined;
      return new Promise(() => {
        /* never resolves — simulates a stream still in flight at unmount */
      });
    });
    const { unmount } = render(<EligibilityChat appointmentId={7} />);
    await open();
    await sendMessage("check now");

    expect(capturedSignal?.aborted).toBe(false);
    unmount();

    expect(capturedSignal?.aborted).toBe(true);
  });
});

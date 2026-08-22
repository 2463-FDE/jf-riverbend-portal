import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// The provenance label is the client's requirement, and it is the one thing on
// this panel a patient cannot infer from anything else on screen. A fallback
// that renders without saying so reads as something the assistant wrote.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import AgentSummaryPanel from "./AgentSummaryPanel";
import { apiFetch } from "../lib/session";

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

const APPROVED = {
  available: true,
  version: 2,
  provenance_label: "fallback",
  generated_text: "Results are shown exactly as the laboratory reported them.",
  citations: [
    { source_id: "POL-001", source_version: "2026-08-01", citation_id: "POL-001@2026-08-01", category: "policy" },
  ],
};

describe("the patient's approved summary panel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the provenance label, version and citation beside the approved text", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok(APPROVED));

    render(<AgentSummaryPanel />);

    const label = await screen.findByText("fallback");
    expect(label).toHaveAttribute("data-provenance", "fallback");
    // ...and in words, because "fallback" alone means nothing to a patient.
    expect(screen.getByText(/not by the AI assistant/i)).toBeInTheDocument();
    expect(screen.getByText(APPROVED.generated_text)).toBeInTheDocument();
    expect(screen.getByText(/Version 2/)).toBeInTheDocument();
    expect(screen.getByText(/POL-001 \(version 2026-08-01, policy\)/)).toBeInTheDocument();
  });

  it("renders no text at all when nothing is approved", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ available: false, version: null, provenance_label: null, generated_text: null, citations: [] }));

    render(<AgentSummaryPanel />);

    expect(await screen.findByText(/no approved summary on your record yet/i)).toBeInTheDocument();
    expect(screen.queryByText(APPROVED.generated_text)).not.toBeInTheDocument();
  });
});

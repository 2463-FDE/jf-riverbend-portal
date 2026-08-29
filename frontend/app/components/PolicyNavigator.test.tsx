import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// w-9-2-planner P3 minimal UI: a question input, a truthful provider/corpus
// label, a cited response, and refusal/error display — nothing else.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import PolicyNavigator from "./PolicyNavigator";
import { apiFetch } from "../lib/session";

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function ask(question: string) {
  fireEvent.change(screen.getByLabelText(/ask how an approved workflow/i), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: /ask/i }));
}

describe("the policy navigator", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows a cited answer with its truthful provider label", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      ok({
        answer: "Coverage stays active for the plan year [SRC-001@1.0#overview].",
        citations: [
          { citation_id: "SRC-001@1.0#overview", source_id: "SRC-001", source_version: "1.0",
            title: "Coverage Guide", section_id: "overview" },
        ],
        label: "real",
        termination_reason: "answered",
      })
    );

    render(<PolicyNavigator />);
    ask("How long does coverage last?");

    expect(await screen.findByText(/answered live by the policy navigator/i)).toBeInTheDocument();
    expect(screen.getByText(/Coverage stays active for the plan year/)).toBeInTheDocument();
    expect(screen.getByText(/Coverage Guide \(version 1\.0, overview\)/)).toBeInTheDocument();
  });

  it("shows the synthetic-corpus notice at all times, not only after an answer", () => {
    render(<PolicyNavigator />);

    expect(screen.getByText(/synthetic training corpus/i)).toBeInTheDocument();
  });

  it("displays a no-evidence refusal without any citations", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      ok({
        answer: "I found no approved policy evidence for this question within your authorized scope.",
        citations: [], label: "real", termination_reason: "no_evidence",
      })
    );

    render(<PolicyNavigator />);
    ask("An unrelated question");

    const answer = await screen.findByText(/no approved policy evidence/i);
    expect(answer).toHaveAttribute("data-termination", "no_evidence");
    expect(screen.queryByText(/where this came from/i)).not.toBeInTheDocument();
  });

  it("displays a fallback label and message when the provider is unavailable", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      ok({
        answer: "I couldn't reach the policy navigator just now. Please try again in a moment.",
        citations: [], label: "fallback", termination_reason: "provider_error",
      })
    );

    render(<PolicyNavigator />);
    ask("Any question");

    expect(await screen.findByText(/could not reach a live model/i)).toBeInTheDocument();
    expect(screen.getByText(/couldn't reach the policy navigator/i)).toHaveAttribute("role", "alert");
  });

  it("displays the safe-step-limit message for max_turns, never the provider-unreachable wording", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      ok({
        answer: "I wasn't able to finish researching this within the allowed number of steps. Please try a narrower question.",
        citations: [], label: "fallback", termination_reason: "max_turns",
      })
    );

    render(<PolicyNavigator />);
    ask("A question that loops");

    const answer = await screen.findByText(/wasn't able to finish researching/i);
    expect(answer).toHaveAttribute("data-termination", "max_turns");
    expect(answer).toHaveAttribute("role", "alert");
    expect(screen.getByText(/safe step limit/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not reach a live model/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/where this came from/i)).not.toBeInTheDocument();
  });

  it("displays the citation-invalid safety refusal distinctly, never as an answered result", async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      ok({
        answer: "I can't show that answer safely — it referenced policy text that wasn't actually retrieved.",
        citations: [], label: "fallback", termination_reason: "citation_invalid",
      })
    );

    render(<PolicyNavigator />);
    ask("A question");

    const answer = await screen.findByText(/can't show that answer safely/i);
    expect(answer).toHaveAttribute("data-termination", "citation_invalid");
    expect(answer).toHaveAttribute("role", "alert");
  });

  it("rejects a blank question locally without calling the server", () => {
    render(<PolicyNavigator />);

    fireEvent.click(screen.getByRole("button", { name: /ask/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(/enter a question/i);
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("shows a sign-in error on 401", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false, status: 401, json: async () => ({}) } as Response);

    render(<PolicyNavigator />);
    ask("Anything");

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/not signed in/i));
  });
});

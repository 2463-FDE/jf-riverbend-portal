import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// The content rules are enforced server-side, but this page is the last thing
// between them and a patient — so what is asserted here is that it renders
// what it was given and adds nothing. The failure this guards against is a
// well-meaning UI that "helps" by labelling a value, which would reintroduce
// exactly the categorization the client ruled out.

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  getUser: () => ({ username: "patient-1737", role: "patient" }),
}));

import MyResultsPage from "./page";
import { apiFetch } from "../lib/session";

function ok(items: unknown[]) {
  return { ok: true, status: 200, json: async () => ({ patient_id: 1737, items }) } as Response;
}

const SINGLE = {
  record_id: 377,
  title: "TSH",
  date: "2026-05-19",
  shape: "single_value",
  quote: "2.3 mIU/L.",
  reference_range: "0.4-4.0 mIU/L",
  change: null,
  refusal_reason: null,
  source_record_ids: [377],
};

const PANEL = {
  record_id: 374,
  title: "Basic metabolic",
  date: "2026-05-19",
  shape: "panel",
  quote: "Na 140, K 4.1, Cr 0.9.",
  reference_range: "Na 135-145; K 3.5-5.1",
  change: null,
  refusal_reason: null,
  source_record_ids: [374],
};

const REFUSED = {
  record_id: 376,
  title: "Visit note",
  date: "2026-05-19",
  shape: "unquotable",
  quote: null,
  reference_range: null,
  change: null,
  refusal_reason: "This result is written as a note rather than a measurement.",
  source_record_ids: [376],
};

describe("a patient reading their own results", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the value exactly as the report recorded it", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok([SINGLE]));
    render(<MyResultsPage />);

    // Verbatim, including the trailing period — not reformatted or rounded.
    expect(await screen.findByText("2.3 mIU/L.")).toBeInTheDocument();
  });

  it("attributes the reference range to the report rather than stating it as fact", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok([SINGLE]));
    render(<MyResultsPage />);

    expect(await screen.findByText("0.4-4.0 mIU/L")).toBeInTheDocument();
    expect(screen.getByText(/as printed on the report/i)).toBeInTheDocument();
  });

  it("never labels a value as normal or abnormal on its own", async () => {
    // The categorization the client ruled out. If the report did not print the
    // word, it must not appear — so a value with NO range shows no verdict.
    vi.mocked(apiFetch).mockResolvedValue(
      ok([{ ...SINGLE, reference_range: null }])
    );
    render(<MyResultsPage />);

    await screen.findByText("2.3 mIU/L.");
    expect(screen.queryByText(/\bnormal\b/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\babnormal\b/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/\bhigh\b|\blow\b/i)).not.toBeInTheDocument();
  });

  it("shows a panel's numbers rather than withholding the result", async () => {
    // Over-refusing a panel is the failure the client named directly.
    vi.mocked(apiFetch).mockResolvedValue(ok([PANEL]));
    render(<MyResultsPage />);

    expect(await screen.findByText("Na 140, K 4.1, Cr 0.9.")).toBeInTheDocument();
    expect(screen.getByText(/several measurements/i)).toBeInTheDocument();
  });

  it("shows the refusal text instead of a value when there is no clean quote", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok([REFUSED]));
    render(<MyResultsPage />);

    expect(await screen.findByText(/written as a note/i)).toBeInTheDocument();
  });

  it("describes a change in neutral words and links it to its source", async () => {
    // "Higher", not "worse". Direction describes the number; whether that is
    // good or bad is a clinical judgment this feature does not make.
    vi.mocked(apiFetch).mockResolvedValue(
      ok([
        {
          ...SINGLE,
          change: {
            direction: "up",
            delta: "0.4",
            unit: "%",
            from_value: "5.8",
            from_record_id: 375,
            from_date: "2026-01-11",
          },
        },
      ])
    );
    render(<MyResultsPage />);

    expect(await screen.findByText(/higher than last time/i)).toBeInTheDocument();
    expect(screen.getByText(/result #375/)).toBeInTheDocument();
    expect(screen.queryByText(/worse|better|improv/i)).not.toBeInTheDocument();
  });

  it("tells a patient with no results that there are none, rather than showing an error", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok([]));
    render(<MyResultsPage />);

    expect(await screen.findByText(/no results on your record yet/i)).toBeInTheDocument();
  });

  it("does not reveal whether a rejection was a wrong account or no session", async () => {
    // A staff account hitting this page and an unauthenticated one get the
    // same answer.
    vi.mocked(apiFetch).mockResolvedValue({ ok: false, status: 403, json: async () => ({}) } as Response);
    render(<MyResultsPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not signed in to a patient account/i);
  });

  it("does not show stale results when the request fails", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false, status: 503, json: async () => ({}) } as Response);
    render(<MyResultsPage />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.queryByText("2.3 mIU/L.")).not.toBeInTheDocument();
  });
});

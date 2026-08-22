import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Every case on this screen is content a patient asked for and did not get,
// and both buttons have a real consequence for them. So what is asserted here
// is mostly about that consequence being visible and hard to trigger by
// accident — not about the list rendering.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import ReviewQueuePage from "./page";
import { apiFetch } from "../lib/session";

const CASE = {
  id: 7,
  patient_id: 1737,
  record_id: 376,
  state: "pending",
  reason: "no clean quote",
  created_at: "2026-08-15T10:00:00Z",
  record_title: "Visit note",
  record_kind: "note",
  record_body: "Penicillin allergy confirmed. Switched to alternative.",
  record_date: "2026-05-19",
};

function queue(items: unknown[]) {
  return { ok: true, status: 200, json: async () => ({ items }) } as Response;
}
function decided(patient_visible: boolean) {
  return { ok: true, status: 200, json: async () => ({ patient_visible }) } as Response;
}
function identity(name: string) {
  return { ok: true, status: 200, json: async () => ({ name }) } as Response;
}

async function renderWithOneCase() {
  // Two calls for one rendered case: the queue itself, then the case card's
  // own identity lookup (2026-08-22 — the combined "{Name} — Patient ID {id}"
  // display every case now shows).
  vi.mocked(apiFetch)
    .mockResolvedValueOnce(queue([CASE]))
    .mockResolvedValueOnce(identity("Priya Khan"));
  render(<ReviewQueuePage />);
  return screen.findByText(CASE.record_body);
}

describe("deciding a case", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the record's own text, not a summary of it", async () => {
    // Approving something you have not read is the failure this screen exists
    // to prevent, so the source text has to be on screen before any button is.
    await renderWithOneCase();
    expect(screen.getByText(CASE.record_body)).toBeInTheDocument();
  });

  it("does not act on the first click — the decision is confirmed", async () => {
    await renderWithOneCase();

    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));

    // Two calls so far: the initial load and the case's identity lookup.
    // Nothing has been decided.
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
  });

  it("says what will happen to the patient before it happens", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));

    expect(await screen.findByText(/patient will see this record's text/i)).toBeInTheDocument();
  });

  it("spells out the consequence of keeping it withheld too", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /keep withheld/i }));

    expect(await screen.findByText(/continue to see a message/i)).toBeInTheDocument();
  });

  it("can be cancelled without deciding anything", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));
    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(2);
  });

  it("sends the decision only after confirmation", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));

    vi.mocked(apiFetch).mockResolvedValueOnce(decided(true));
    fireEvent.click(await screen.findByRole("button", { name: /yes, continue/i }));

    await waitFor(() =>
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/review-queue/7/decision",
        expect.objectContaining({ method: "POST", body: JSON.stringify({ decision: "approved" }) })
      )
    );
  });

  it("reports the outcome in terms of what the patient can now see", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /keep withheld/i }));
    vi.mocked(apiFetch).mockResolvedValueOnce(decided(false));
    fireEvent.click(await screen.findByRole("button", { name: /yes, continue/i }));

    expect(await screen.findByText(/will not see that record/i)).toBeInTheDocument();
  });
});

describe("when a decision cannot be recorded", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not claim success when the request fails", async () => {
    // A clinician who believes they released a record, and did not, will not
    // revisit it — so a failed decision must never read as a completed one.
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));

    vi.mocked(apiFetch).mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({}) } as Response);
    fireEvent.click(await screen.findByRole("button", { name: /yes, continue/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/nothing has changed/i);
    expect(screen.queryByText(/can now see/i)).not.toBeInTheDocument();
  });

  it("refreshes and explains when someone else already decided the case", async () => {
    await renderWithOneCase();
    fireEvent.click(screen.getByRole("button", { name: /release to patient/i }));

    vi.mocked(apiFetch)
      .mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({}) } as Response)
      .mockResolvedValueOnce(queue([]));
    fireEvent.click(await screen.findByRole("button", { name: /yes, continue/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/already decided by someone else/i);
    await waitFor(() => expect(screen.getByText(/nothing is waiting for review/i)).toBeInTheDocument());
  });

  it("says plainly when the caller may not see the queue", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({ ok: false, status: 403, json: async () => ({}) } as Response);
    render(<ReviewQueuePage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/do not have access/i);
  });

  it("shows an empty queue as empty, not as an error", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(queue([]));
    render(<ReviewQueuePage />);

    expect(await screen.findByText(/nothing is waiting for review/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

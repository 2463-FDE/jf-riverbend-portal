import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RoiPage from "./page";
import type { RoiRequest } from "../lib/types";

// W10 Final 2 Stage 1: the fulfill proxy route used to send an empty body
// ({}), which roi-service's required authorization_id field always 422'd —
// and the gateway (before this stage) flattened that into a false 200, so
// the UI showed "marked fulfilled" for a fulfillment that never happened.
// These tests pin the real contract: the UI must collect an authorization
// id, send it, and show the backend's real failure text on a non-2xx
// response rather than a generic success/failure message.

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "../lib/session";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function pendingRequest(id: number): RoiRequest {
  return {
    id,
    patient_id: 1042,
    recipient: "Dr. Chen",
    recipient_type: "Healthcare provider",
    purpose: "Continuity of care",
    date_range_start: "",
    date_range_end: "",
    status: "pending",
  };
}

describe("RoiPage fulfillment", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("refuses to fulfill without an authorization id, and never calls the API", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(jsonResponse([pendingRequest(33)]));
    render(<RoiPage />);
    await waitFor(() => expect(screen.getByText(/#33/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /fulfill/i }));

    await waitFor(() =>
      expect(screen.getByText(/enter the id of a reviewed, valid authorization/i)).toBeInTheDocument()
    );
    // Only the initial list load happened — no fulfill call was attempted.
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it("sends the entered authorization_id in the fulfill request body", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(jsonResponse([pendingRequest(33)]))
      .mockResolvedValueOnce(
        jsonResponse({ request_id: 33, patient_id: 1042, status: "fulfilled", disclosure_id: 1, records: [] })
      )
      .mockResolvedValueOnce(jsonResponse([{ ...pendingRequest(33), status: "fulfilled" }]));

    render(<RoiPage />);
    await waitFor(() => expect(screen.getByText(/#33/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/authorization id for request #33/i), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: /fulfill/i }));

    await waitFor(() => expect(screen.getByText(/marked fulfilled/i)).toBeInTheDocument());

    const fulfillCall = vi.mocked(apiFetch).mock.calls[1];
    expect(fulfillCall[0]).toBe("/api/roi/requests/33/fulfill");
    expect(JSON.parse(fulfillCall[1]!.body as string)).toEqual({ authorization_id: 7 });
  });

  it("shows the backend's real failure text instead of a false success", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce(jsonResponse([pendingRequest(33)]))
      .mockResolvedValueOnce(jsonResponse({ detail: "authorization has expired" }, false));

    render(<RoiPage />);
    await waitFor(() => expect(screen.getByText(/#33/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/authorization id for request #33/i), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: /fulfill/i }));

    await waitFor(() => expect(screen.getByText(/authorization has expired/i)).toBeInTheDocument());
    expect(screen.queryByText(/marked fulfilled/i)).not.toBeInTheDocument();
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// The name sits beside a bare patient ID on two staff screens. What matters is
// not that it renders — it is that it never renders the WRONG patient's name,
// and that a denied lookup reads as a boundary rather than a broken screen.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import PatientName from "./PatientName";
import { apiFetch } from "../lib/session";

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}
function err(status: number, body: unknown = {}) {
  return { ok: false, status, json: async () => body } as Response;
}

describe("the patient name beside the ID", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the name for a loaded patient", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ id: 1042, name: "Maria Alvarez" }));

    render(<PatientName patientId="1042" />);

    expect(await screen.findByText("Maria Alvarez")).toBeInTheDocument();
  });

  it("fetches nothing at all when there is no loaded id", () => {
    // The empty id is how both callers clear the name on a keystroke. If this
    // fetched, every character typed would write an audit row server-side.
    render(<PatientName patientId="" />);

    expect(apiFetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("patient-name")).toHaveTextContent(/patient name/i);
  });

  it("holds its space before a name resolves, so the row does not reflow", () => {
    // The box sits between the ID field and the Load button. If it collapsed
    // when empty, a name arriving would shove the Load button sideways under
    // the cursor.
    vi.mocked(apiFetch).mockResolvedValue(ok({ id: 1042, name: "Maria Alvarez" }));

    render(<PatientName patientId="1042" />);

    expect(screen.getByTestId("patient-name")).toBeInTheDocument();
  });

  it("says the name is unavailable on a denial instead of leaving a gap", async () => {
    // Grants are per-(actor, patient) and sparse: front desk holds 1042 but
    // not 1737. A 403 here is the authorization boundary working, and it
    // reads as a bug unless the screen states it.
    vi.mocked(apiFetch).mockResolvedValue(err(403, { error: "name unavailable" }));

    render(<PatientName patientId="1737" />);

    expect(await screen.findByText(/name unavailable/i)).toBeInTheDocument();
  });

  it("never renders one patient's name against another's id", async () => {
    // The response for the first id lands AFTER the id has moved on. Applying
    // it would put Maria's name above Daniel's chart.
    let releaseFirst: (r: Response) => void = () => {};
    vi.mocked(apiFetch)
      .mockImplementationOnce(() => new Promise<Response>((res) => { releaseFirst = res; }))
      .mockResolvedValue(ok({ id: 1330, name: "Daniel Cho" }));

    const { rerender } = render(<PatientName patientId="1042" />);
    rerender(<PatientName patientId="1330" />);
    releaseFirst(ok({ id: 1042, name: "Maria Alvarez" }));

    expect(await screen.findByText("Daniel Cho")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("Maria Alvarez")).not.toBeInTheDocument()
    );
  });

  it("clears the previous name before the next one loads", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ id: 1042, name: "Maria Alvarez" }));
    const { rerender } = render(<PatientName patientId="1042" />);
    await screen.findByText("Maria Alvarez");

    // A stale name surviving the clear is the failure this guards: both
    // callers pass "" on an id change precisely so nothing is left behind.
    rerender(<PatientName patientId="" />);

    expect(screen.queryByText("Maria Alvarez")).not.toBeInTheDocument();
    expect(screen.getByTestId("patient-name")).toHaveTextContent(/patient name/i);
  });

  it("does not treat a 200 with no name as a name", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ id: 1042, name: null }));

    render(<PatientName patientId="1042" />);

    expect(await screen.findByText(/name unavailable/i)).toBeInTheDocument();
  });
});

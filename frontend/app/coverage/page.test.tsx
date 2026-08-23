import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CoveragePage from "./page";

// This screen must never show a full member id, and must never present a
// simulated/stale/unknown result as active coverage — those are the two
// failure shapes billing staff would actually act on incorrectly.

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

vi.mock("../lib/session", () => ({ apiFetch: vi.fn(), clearSession: vi.fn() }));

import { apiFetch, clearSession } from "../lib/session";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}
function denied(status: number): Response {
  return { ok: false, status, json: async () => ({}) } as Response;
}

const COVERAGE = {
  id: 1,
  patient_id: 1737,
  payer_name: "Acme Health",
  plan_type: "PPO",
  group_number: "GRP-1",
  member_id_masked: "********6789",
  status: "unknown",
  verified_at: null,
  has_member_id: true,
};

function mockRoutes(overrides: Partial<{ coverages: unknown; verify: unknown; name: unknown }> = {}) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.includes("/name")) return ok(overrides.name ?? { id: 1737, name: "Priya Khan" });
    if (url.includes("/verify")) return ok(overrides.verify ?? { category: "simulated", message: "Synthetic training — no payer contacted" });
    if (url.includes("/eligibility-status")) return ok({ category: "unknown", message: "Not yet verified" });
    if (url.includes("/coverages")) return ok(overrides.coverages ?? { items: [COVERAGE] });
    return ok({});
  });
}

describe("Coverage & Eligibility", () => {
  // Round-1 review: without this, an earlier test's clearSession()/replace()
  // calls could satisfy a later test's assertion even if that later test's
  // own code path never actually called them.
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("starts empty and loads coverage only after Load, with the member id masked", async () => {
    mockRoutes();
    render(<CoveragePage />);

    const input = screen.getByLabelText(/patient id/i) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/coverages"))).toBe(false);

    fireEvent.change(input, { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(await screen.findByText("Acme Health")).toBeInTheDocument();
    expect(screen.getByText(/member id \*+6789/i)).toBeInTheDocument();
    expect(screen.queryByText(/ABC123456789/)).not.toBeInTheDocument();
  });

  it("shows a denied message rather than any coverage content", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return denied(403);
      if (url.includes("/coverages")) return denied(403);
      return ok({});
    });
    render(<CoveragePage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(await screen.findByText(/not authorized/i)).toBeInTheDocument();
    expect(screen.queryByText("Acme Health")).not.toBeInTheDocument();
  });

  it("shows an empty state when the patient has no coverage on file", async () => {
    mockRoutes({ coverages: { items: [] } });
    render(<CoveragePage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(await screen.findByText(/no coverage on file/i)).toBeInTheDocument();
  });

  it("labels a simulated verification explicitly and never as active", async () => {
    mockRoutes();
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await screen.findByText("Acme Health");

    fireEvent.click(screen.getByRole("button", { name: /request verification/i }));

    expect((await screen.findAllByText(/synthetic training/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/^active$/i)).not.toBeInTheDocument();
  });

  it("shows a retry control only when the backend says retry is allowed", async () => {
    mockRoutes({ verify: { category: "unavailable", message: "Temporarily unavailable", can_retry: true } });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await screen.findByText("Acme Health");

    expect(screen.queryByRole("button", { name: /^retry$/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /request verification/i }));

    expect(await screen.findByRole("button", { name: /^retry$/i })).toBeInTheDocument();
  });

  it("disables requesting a verification when there is no member id on file", async () => {
    mockRoutes({ coverages: { items: [{ ...COVERAGE, member_id_masked: null, has_member_id: false }] } });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await screen.findByText(/no member id on file/i);
    expect(screen.getByRole("button", { name: /request verification/i })).toBeDisabled();
  });

  it("shows a plain error when the coverage list fails to load", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return ok({ id: 1737, name: "Priya Khan" });
      if (url.includes("/coverages")) return denied(500);
      return ok({});
    });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(await screen.findByText(/could not load coverage/i)).toBeInTheDocument();
  });

  // P0 (w-9-2-planner): a 401 is the gateway session itself expiring — not a
  // coverage/backend failure — and must never render as "Could not load
  // coverage." (indistinguishable from a real outage) or "not authorized"
  // (indistinguishable from a real per-patient denial).
  it("clears the session and redirects to sign-in on an expired session (401), not a generic load error", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/coverages")) return denied(401);
      return ok({});
    });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(clearSession).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
    expect(screen.queryByText(/could not load coverage/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/not authorized/i)).not.toBeInTheDocument();
  });

  it("also treats an expired session as expired, not a failure, when requesting a verification", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes("/name")) return ok({ id: 1737, name: "Priya Khan" });
      if (url.includes("/verify")) return denied(401);
      if (url.includes("/coverages")) return ok({ items: [COVERAGE] });
      return ok({});
    });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await screen.findByText("Acme Health");

    fireEvent.click(screen.getByRole("button", { name: /request verification/i }));

    await waitFor(() => expect(clearSession).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("also treats an expired session as expired, not a failure, when checking status", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return ok({ id: 1737, name: "Priya Khan" });
      if (url.includes("/eligibility-status")) return denied(401);
      if (url.includes("/coverages")) return ok({ items: [COVERAGE] });
      return ok({});
    });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await screen.findByText("Acme Health");

    fireEvent.click(screen.getByRole("button", { name: /^check status$/i }));

    await waitFor(() => expect(clearSession).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("also treats an expired session as expired, not a failure, on retry", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return ok({ id: 1737, name: "Priya Khan" });
      if (url.includes("/eligibility-retry")) return denied(401);
      // A can_retry:true verify result is what makes the Retry button appear.
      if (url.includes("/verify")) return ok({ category: "unavailable", message: "Temporarily unavailable", can_retry: true });
      if (url.includes("/coverages")) return ok({ items: [COVERAGE] });
      return ok({});
    });
    render(<CoveragePage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await screen.findByText("Acme Health");
    fireEvent.click(screen.getByRole("button", { name: /request verification/i }));
    await screen.findByRole("button", { name: /^retry$/i });

    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));

    await waitFor(() => expect(clearSession).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });
});

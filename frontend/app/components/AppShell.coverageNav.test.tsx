import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Coverage & Eligibility (W9.3) replaces the disabled "Billing" placeholder
// for the roles that actually hold billing.read (config/roles.yaml):
// front_desk, billing, management, and the deprecated staff role — not
// clinician/nursing_ma/lab/roi_clerk/scheduler/it_admin.
const replace = vi.fn();
const ROUTER = { replace };

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ROUTER,
}));

function mockUser(user: { username: string; full_name: string; role: string }) {
  vi.doMock("../lib/session", () => ({
    apiFetch: vi.fn(),
    clearSession: vi.fn(),
    getToken: () => "tok",
    getUser: () => user,
  }));
}

describe("Coverage & Eligibility nav link", () => {
  it("is offered to front_desk, and never labelled Billing", async () => {
    vi.resetModules();
    mockUser({ username: "frontdesk", full_name: "Front Desk", role: "front_desk" });
    const { default: AppShell } = await import("./AppShell");

    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    expect(await screen.findByRole("link", { name: /coverage & eligibility/i })).toHaveAttribute(
      "href",
      "/coverage"
    );
    expect(screen.queryByText(/^billing$/i)).not.toBeInTheDocument();
  });

  it("is withheld from a role holding no billing permission", async () => {
    vi.resetModules();
    mockUser({ username: "drkim", full_name: "Dr. Grace Kim", role: "clinician" });
    const { default: AppShell } = await import("./AppShell");

    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /review queue/i });
    expect(screen.queryByRole("link", { name: /coverage & eligibility/i })).not.toBeInTheDocument();
  });
});

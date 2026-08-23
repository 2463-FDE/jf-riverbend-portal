import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Messages (W9.2) is offered to the same two roles that already see Review
// queue — clinician and nursing_ma, the roles holding messages.read in
// config/roles.yaml — and withheld from a role that does not, the same
// courtesy-not-a-control property AppShell.patientNav.test.tsx documents.
//
// Stable mock references, same reasoning as the other AppShell nav test
// files: a fresh object per call spins AppShell's hydrate effect forever.
const replace = vi.fn();
const ROUTER = { replace };

vi.mock("next/navigation", () => ({
  usePathname: () => "/review-queue",
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

describe("Messages nav link for staff roles", () => {
  it("is offered to a clinician, alongside the review queue", async () => {
    vi.resetModules();
    mockUser({ username: "drkim", full_name: "Dr. Grace Kim", role: "clinician" });
    const { default: AppShell } = await import("./AppShell");

    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    expect(await screen.findByRole("link", { name: /^messages$/i })).toHaveAttribute("href", "/messages");
    expect(screen.getByRole("link", { name: /review queue/i })).toBeInTheDocument();
  });

  it("is withheld from a role that holds no messages permission", async () => {
    vi.resetModules();
    mockUser({ username: "frontdesk", full_name: "Front Desk", role: "front_desk" });
    const { default: AppShell } = await import("./AppShell");

    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /appointments/i });
    expect(screen.queryByRole("link", { name: /^messages$/i })).not.toBeInTheDocument();
  });
});

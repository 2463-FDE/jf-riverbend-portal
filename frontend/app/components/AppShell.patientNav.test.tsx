import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// A patient signing in must not be shown the staff menu.
//
// Every entry in the default navigation is a staff route that the `patient`
// role is refused — it holds no staff permission at all — so rendering that
// menu for a patient produces five links that each fail. This is a usability
// property, not a security one: the gateway and records-service refuse those
// routes regardless of what is drawn here, and this file does not pretend
// otherwise.
//
// The mocks below return STABLE references on purpose. AppShell's hydrate
// effect depends on [isLogin, pathname, router] and calls setUser(getUser());
// a fresh object from either mock on every call changes the dependency each
// render and spins forever. (Learned the hard way — see AppShell.test.tsx.)
const replace = vi.fn();
const ROUTER = { replace };
const PATIENT = { username: "patient-1737", full_name: "A Patient", role: "patient" };

vi.mock("next/navigation", () => ({
  usePathname: () => "/my-results",
  useRouter: () => ROUTER,
}));

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  clearSession: vi.fn(),
  getToken: () => "tok-patient",
  getUser: () => PATIENT,
}));

import AppShell from "./AppShell";

describe("navigation for a patient account", () => {
  it("shows the patient their results and nothing else", async () => {
    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    expect(await screen.findByRole("link", { name: /your results/i })).toBeInTheDocument();
  });

  it("offers a Home link to the patient landing page (W9.1)", async () => {
    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    const home = await screen.findByRole("link", { name: /^home$/i });
    expect(home).toHaveAttribute("href", "/");
  });

  it("does not show the hardcoded '1 new' notification dot — there is no real unread source yet", async () => {
    const { container } = render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /your results/i });
    expect(container.querySelector(".rb-iconbtn__dot")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^notifications$/i })).toBeInTheDocument();
  });

  it("does not offer staff destinations a patient cannot open", async () => {
    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /your results/i });

    for (const label of [/records/i, /intake/i, /release of information/i, /appointments/i, /dashboard/i]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });

  it("does not advertise staff features that are still coming", async () => {
    // "Messages" and "Billing" are staff-side placeholders. Showing a patient
    // features they are not the audience for promises something untrue.
    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /your results/i });
    expect(screen.queryByText(/^billing$/i)).not.toBeInTheDocument();
  });
});

describe("the review queue link", () => {
  it("is not offered to a patient", async () => {
    // A patient cannot decide a review — the role holds no staff permission —
    // so offering the link would be a dead end at best.
    render(
      <AppShell>
        <p>content</p>
      </AppShell>
    );

    await screen.findByRole("link", { name: /your results/i });
    expect(screen.queryByRole("link", { name: /review queue/i })).not.toBeInTheDocument();
  });
});

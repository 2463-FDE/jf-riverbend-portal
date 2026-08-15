import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// /activate is public by necessity: the patient redeeming an invitation code
// has no account yet — creating one is the whole point of the page.
//
// This file exists because that was broken in the running app while every
// test passed. The shell exempted only /login, so an unauthenticated patient
// opening /activate was redirected to the sign-in screen and could never
// redeem a code. Nothing caught it, because the integration tests drove the
// API directly and never went through a browser.
//
// Stable mock references — a fresh object per call changes the hydrate
// effect's dependencies every render and spins forever.
const replace = vi.fn();
const ROUTER = { replace };

let currentPath = "/activate";

vi.mock("next/navigation", () => ({
  usePathname: () => currentPath,
  useRouter: () => ROUTER,
}));

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  clearSession: vi.fn(),
  getToken: () => null,        // nobody is signed in
  getUser: () => null,
}));

import AppShell from "./AppShell";

describe("routes reachable without an account", () => {
  beforeEach(() => {
    replace.mockClear();
    currentPath = "/activate";
  });

  it("does not bounce an unauthenticated visitor away from /activate", async () => {
    render(
      <AppShell>
        <p>redeem your code</p>
      </AppShell>
    );

    expect(await screen.findByText("redeem your code")).toBeInTheDocument();
    await waitFor(() => expect(replace).not.toHaveBeenCalled());
  });

  it("renders /activate without the signed-in navigation around it", async () => {
    // A patient redeeming a code has no chart, no appointments and no staff
    // destinations — the shell would be furniture around an empty account.
    render(
      <AppShell>
        <p>redeem your code</p>
      </AppShell>
    );

    await screen.findByText("redeem your code");
    expect(screen.queryByRole("link", { name: /records/i })).not.toBeInTheDocument();
  });

  it("still bounces an unauthenticated visitor away from a private route", async () => {
    // The guard must not have been loosened into "no guard at all".
    currentPath = "/records";
    render(
      <AppShell>
        <p>chart</p>
      </AppShell>
    );

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });
});

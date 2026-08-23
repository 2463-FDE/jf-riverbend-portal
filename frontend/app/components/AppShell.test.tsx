import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The client's ask was "a logout that actually ends the session server-side."
// signOut() used to wrap the gateway call in an empty catch and clear local
// storage regardless, so a failed logout showed a signed-out screen while the
// session stayed valid — on a machine the next person was about to use. These
// tests pin the corrected order: confirm with the server, THEN clear locally.

// These stubs must be STABLE references. AppShell's hydrate effect depends on
// [isLogin, pathname, router] and calls setUser(getUser()); returning a fresh
// object from either mock on every call makes the dependency change each
// render and spins forever.
const replace = vi.fn();
const ROUTER = { replace };
const USER = { username: "frontdesk", full_name: "Front Desk", role: "staff" };

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ROUTER,
}));

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  clearSession: vi.fn(),
  getToken: () => "tok-abc",
  getUser: () => USER,
}));

import AppShell from "./AppShell";
import { apiFetch, clearSession } from "../lib/session";

function openUserMenu() {
  // The sign-out control lives behind the user menu.
  const toggles = document.querySelectorAll<HTMLButtonElement>("button.rb-usermenu__btn");
  fireEvent.click(toggles[0]);
}

async function clickSignOut() {
  openUserMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: /sign out/i }));
}

describe("signing out of a shared workstation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("clears the local session and leaves once the gateway confirms", async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);

    render(<AppShell><div /></AppShell>);
    await clickSignOut();

    await waitFor(() => expect(clearSession).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("does NOT clear the local session when the gateway call fails", async () => {
    // The defect: clearing here strands a live session on the server.
    vi.mocked(apiFetch).mockRejectedValue(new Error("network down"));

    render(<AppShell><div /></AppShell>);
    await clickSignOut();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(clearSession).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalledWith("/login");
  });

  it("does NOT clear the local session when the gateway returns an error status", async () => {
    // A 503 from /logout means Redis was unreachable and nothing was ended.
    vi.mocked(apiFetch).mockResolvedValue({ ok: false, status: 503 } as Response);

    render(<AppShell><div /></AppShell>);
    await clickSignOut();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(clearSession).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalledWith("/login");
  });

  it("tells the user they are still signed in, and what to do", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("network down"));

    render(<AppShell><div /></AppShell>);
    await clickSignOut();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/still signed in/i);
    expect(alert.textContent).toMatch(/try again/i);
  });
});

describe("the notification dot for a non-patient session", () => {
  it("is unchanged — still shown, since suppressing it (W9.1) is scoped to patient sessions only", () => {
    const { container } = render(<AppShell><div /></AppShell>);

    expect(container.querySelector(".rb-iconbtn__dot")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /notifications \(1 new\)/i })).toBeInTheDocument();
  });
});

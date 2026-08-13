"use client";

import type { PortalUser } from "./types";

// Client-side session helpers.
//
// Shared-workstation fix: the token used to live in localStorage, which
// survives closing the browser and is shared across every tab on that
// origin. On a shared clinical workstation that means the next person to
// open the browser is still signed in as whoever used it last. It now lives
// in sessionStorage, which the browser clears when the tab closes.
//
// The trade-off, deliberately accepted: sessionStorage is per-tab, so opening
// the portal in a second tab requires signing in again. On a shared machine
// that is the safer default.
//
// Server-side expiry is enforced independently of anything here — see
// services/gateway/security.py for the idle and absolute TTLs. Clearing
// browser storage is not a logout on its own; only the gateway can end a
// session, which is why signOut waits for it to confirm.

const TOKEN_KEY = "riverbend.token";
const USER_KEY = "riverbend.user";

function store(): Storage | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage;
}

export function getToken(): string | null {
  return store()?.getItem(TOKEN_KEY) ?? null;
}

export function getUser(): PortalUser | null {
  const raw = store()?.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as PortalUser;
  } catch {
    return null;
  }
}

export function setSession(token: string, user: PortalUser): void {
  const s = store();
  if (!s) return;
  s.setItem(TOKEN_KEY, token);
  s.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession(): void {
  const s = store();
  if (!s) return;
  s.removeItem(TOKEN_KEY);
  s.removeItem(USER_KEY);
  // A token left behind in localStorage by a build from before this change
  // would otherwise outlive every logout on this machine.
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    /* localStorage may be unavailable; the sessionStorage clear above is what matters */
  }
}

// fetch wrapper that attaches the bearer token to our own /api routes. The
// route handlers forward it to the gateway.
export async function apiFetch(
  input: string,
  init: RequestInit = {}
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}

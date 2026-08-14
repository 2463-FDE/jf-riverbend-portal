import { beforeEach, describe, expect, it } from "vitest";

import { clearSession, getToken, getUser, setSession } from "./session";

const TOKEN_KEY = "riverbend.token";
const USER_KEY = "riverbend.user";

const USER = { username: "frontdesk", full_name: "Front Desk", role: "staff" } as never;

describe("session storage on a shared workstation", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it("stores the token in sessionStorage, which the browser clears on close", () => {
    setSession("tok-abc", USER);

    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe("tok-abc");
    expect(getToken()).toBe("tok-abc");
  });

  it("never writes the token to localStorage, which would survive a browser restart", () => {
    // This is the shared-workstation defect: a token in localStorage means the
    // next person to open the browser is still signed in as the last user.
    setSession("tok-abc", USER);

    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(window.localStorage.getItem(USER_KEY)).toBeNull();
  });

  it("clears both the token and the user on sign-out", () => {
    setSession("tok-abc", USER);

    clearSession();

    expect(getToken()).toBeNull();
    expect(getUser()).toBeNull();
    expect(window.sessionStorage.getItem(USER_KEY)).toBeNull();
  });

  it("also clears a token left in localStorage by a pre-fix build", () => {
    // Without this, a token written by the old build outlives every logout on
    // that machine, because nothing would ever remove it again.
    window.localStorage.setItem(TOKEN_KEY, "stale-tok");
    window.localStorage.setItem(USER_KEY, JSON.stringify(USER));

    clearSession();

    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(window.localStorage.getItem(USER_KEY)).toBeNull();
  });

  it("returns null rather than throwing when the stored user is corrupt", () => {
    window.sessionStorage.setItem(USER_KEY, "{not json");

    expect(getUser()).toBeNull();
  });

  it("reports no token when nothing is stored", () => {
    expect(getToken()).toBeNull();
    expect(getUser()).toBeNull();
  });
});

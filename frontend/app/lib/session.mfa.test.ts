import { beforeEach, describe, expect, it } from "vitest";

import { clearPendingMfaChallenge, getPendingMfaChallenge, setPendingMfaChallenge } from "./session";

describe("pending MFA challenge storage (w8-planner-2 rollout)", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("returns null when nothing is pending", () => {
    expect(getPendingMfaChallenge()).toBeNull();
  });

  it("round-trips the challenge token and enrollment_required flag", () => {
    setPendingMfaChallenge("chal-abc", true);

    expect(getPendingMfaChallenge()).toEqual({ challengeToken: "chal-abc", enrollmentRequired: true });
  });

  it("defaults enrollmentRequired to false when set false", () => {
    setPendingMfaChallenge("chal-abc", false);

    expect(getPendingMfaChallenge()).toEqual({ challengeToken: "chal-abc", enrollmentRequired: false });
  });

  it("clears both keys", () => {
    setPendingMfaChallenge("chal-abc", true);

    clearPendingMfaChallenge();

    expect(getPendingMfaChallenge()).toBeNull();
    expect(window.sessionStorage.getItem("riverbend.mfa.challenge")).toBeNull();
    expect(window.sessionStorage.getItem("riverbend.mfa.enrollmentRequired")).toBeNull();
  });

  it("never writes the challenge token to localStorage", () => {
    setPendingMfaChallenge("chal-abc", true);

    expect(window.localStorage.getItem("riverbend.mfa.challenge")).toBeNull();
  });
});

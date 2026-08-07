import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Minimal test runner for component-level regression tests. This repo has
// no other frontend test framework configured (see CLAUDE.md) — this file
// exists specifically to cover the stale-patient-panel regression added
// alongside the Week 6 records reconciliation view.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["app/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});

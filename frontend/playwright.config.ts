import { defineConfig, devices } from "@playwright/test";

/**
 * Borrower-workflow E2E config (audit follow-up: the previous browser
 * verification was interactive only, never committed -- these are the
 * committed, repeatable replacements).
 *
 * Required environment variables (documented here, never committed with
 * real values):
 *   E2E_BASE_URL   - the running frontend, default http://localhost:3000
 *   DATABASE_URL   - same Postgres the backend uses, for the DB-state
 *                    assertions ("exactly one application/decision/offer/
 *                    loan") AND for fixture setup, which WRITES. This said
 *                    "read-only" and was wrong for seven spec files; see
 *                    e2e/README.md for what writes what. Point it at a
 *                    database you are willing to have modified.
 *                    `fee-waiver-clarity` writes an append-only ledger entry
 *                    it cannot undo, so it consumes a loan per test -- RF-27.
 *
 * Local command: `npm run test:e2e` (frontend, gateway, and the full
 * backend stack + Postgres must already be running -- see
 * frontend/e2e/README.md).
 *
 * Diagnostics/safety: `trace` is deliberately left off. Playwright's trace
 * viewer records full request/response detail including headers -- this
 * suite calls endpoints that take a real bearer-style credential
 * (X-Offer-Accept-Token) in a header, and a trace file is exactly the kind
 * of artifact that could otherwise end up holding it. Screenshots ARE
 * captured on failure: the raw token is never rendered as visible text on
 * any page (it only ever exists as an outbound fetch header), so a
 * screenshot of the rendered page cannot contain it. Both
 * playwright-report/ and test-results/ are gitignored -- never committed.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["html", { open: "never" }], ["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://localhost:3000",
    trace: "off",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});

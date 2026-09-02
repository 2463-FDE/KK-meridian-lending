import { test, expect } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { GATEWAY_URL } from "../lib/api";
import { signInAsStaff, signInAsBorrower } from "./fixtures";

/**
 * The existing Policy Chat is an INTERNAL tool for lending, compliance and
 * underwriting staff — the client's decision, recorded as `docs/DEBT.md` RF-28.
 *
 * anonymous DENY · borrower DENY · csr ALLOW · underwriter ALLOW · admin ALLOW
 *
 * WHY BOTH SURFACES ARE CHECKED HERE. RF-28 was never a bug in one half: the
 * gateway allowed anonymous callers on the stated reasoning that policy Q&A
 * carries no applicant data, while the page had always been staff-gated. Both
 * were defensible and they disagreed, so a borrower refused by the screen could
 * still be answered by the route. Testing only the UI would leave exactly the
 * hole that existed, and testing only the API would not show that the screen
 * still refuses the same people.
 *
 * The API is exercised through the gateway, which is the only path an external
 * caller has. `loan-assistant`'s own route remains reachable from inside the
 * compose network with no token — the SEC-17 trust boundary, deliberately out
 * of scope here and stated in RF-28 rather than implied to be closed.
 *
 * No provider credential is needed: these assert AUTHORIZATION, so the status
 * code is the whole subject. A 500 from a missing model would still prove the
 * caller got past the gate, which is why the allowed roles assert "not refused"
 * rather than 200 — the gate is what is under test, not the answer.
 */

const QUESTION = { question: "What is the origination fee?" };

async function askThroughGateway(request: APIRequestContext, token?: string) {
  return request.post(`${GATEWAY_URL}/assistant/policy-chat`, {
    data: QUESTION,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    failOnStatusCode: false,
  });
}

async function sessionToken(page: Page) {
  return page.evaluate(() => window.localStorage.getItem("meridian.token"));
}

test("an anonymous caller is refused by the API", async ({ request }) => {
  const resp = await askThroughGateway(request);

  expect(resp.status(), "anonymous must not reach the internal policy tool").toBe(401);
});

test("a borrower is refused by the API even with a valid session", async ({
  page,
  request,
}) => {
  // A real session, so this proves the ROLE is what refuses them rather than a
  // missing credential. This request previously returned 200.
  await signInAsBorrower(page);
  const token = await sessionToken(page);
  expect(token, "the borrower session was not established").toBeTruthy();

  const resp = await askThroughGateway(request, token as string);

  expect(resp.status(), "a borrower must not reach the internal policy tool").toBe(403);
});

test("a borrower is refused by the page", async ({ page }) => {
  await signInAsBorrower(page);
  await page.goto("/policy-chat");

  // `RequireRole` sends a non-permitted role to its own role home rather than
  // rendering the screen. The assertion is that the tool is not shown.
  await expect(page.getByRole("heading", { name: "Policy Chat" })).toHaveCount(0);
});

for (const role of ["csr", "underwriter", "admin"] as const) {
  test(`${role} reaches the API`, async ({ page, request }) => {
    await signInAsStaff(page, role);
    const token = await sessionToken(page);
    expect(token, `the ${role} session was not established`).toBeTruthy();

    const resp = await askThroughGateway(request, token as string);

    // Not 200: without a provider credential the answer itself can fail, and
    // that is not what this is about. What matters is that the gate let them
    // through — 401 or 403 would mean it did not.
    expect([401, 403]).not.toContain(resp.status());
  });

  test(`${role} is offered the page`, async ({ page }) => {
    await signInAsStaff(page, role);
    await page.goto("/policy-chat");

    await expect(page.getByRole("heading", { name: "Policy Chat" })).toBeVisible({
      timeout: 20_000,
    });
  });
}

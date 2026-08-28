import { test, expect } from "@playwright/test";

/**
 * If the landing page shows the inherited compliance claims, it shows the
 * qualifier with them.
 *
 * The three badges -- "SOX-controlled", "PCI compliant", "ECOA / Reg B" -- come
 * from the Halcyon baseline (`git blame`: c56240f, 2023-11-02), not from this
 * engagement. Nothing in `specs/` or `adr/` requires them, no accepted
 * requirement mentions the landing page, and `docs/DEBT.md` records them at D25
 * as inherited vendor over-claim.
 *
 * Every current authority contradicts the literal text:
 *
 *   - `README.md` -- "Treat any prior claim of PCI-DSS compliance for this
 *     codebase as false", and SOX / ECOA-Reg B process claims beyond the
 *     decision audit trail are unverified.
 *   - `ARCHITECTURE.md` -- nothing in the repository asserts regulatory
 *     compliance, and several controls are explicitly non-compliant.
 *   - `docs/presentations/2026-08-25-agentic-client-handoff.md` -- "Claims we
 *     must NOT make" lists "PCI compliant" by name. That is the newest
 *     client-facing direction, and it is the one the landing page contradicted.
 *
 * **This test does not exist to preserve the claims.** It preserves the
 * relationship between them: shown together, or not shown at all. Removing the
 * badges entirely is a legitimate future remediation and passes here -- what
 * fails is showing an unsupported claim bare, which is the state that made the
 * first screen of the demo contradict the handoff document.
 */

const INHERITED_CLAIMS = ["SOX-controlled", "PCI compliant", "ECOA / Reg B"];

test("an inherited compliance claim is never shown without its qualifier", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

  const badgeRow = page.locator(".badge-row").first();
  const shown: string[] = [];
  for (const claim of INHERITED_CLAIMS) {
    if (await badgeRow.getByText(claim, { exact: true }).count()) shown.push(claim);
  }

  if (shown.length === 0) {
    // The claims were removed. That is a valid remediation, not a failure, and
    // this test deliberately does not force them back.
    return;
  }

  const qualifier = page.getByTestId("inherited-claims-qualifier");
  await expect(
    qualifier,
    `the landing page shows ${shown.join(", ")} with no visible qualifier -- ` +
      "an unsupported inherited claim must never be presented as Meridian's own",
  ).toBeVisible();

  const text = ((await qualifier.textContent()) ?? "").toLowerCase();
  // The wording may change to fit the UI; the meaning may not.
  expect(text).toContain("inherited");
  expect(/not verified|unverified/.test(text)).toBe(true);
});

test("the qualifier sits with the claims, not somewhere else on the page", async ({
  page,
}) => {
  // A qualifier further down the page, or below the fold, does not qualify
  // anything -- the two have to be read together.
  await page.goto("/");
  const badgeRow = page.locator(".badge-row").first();
  await expect(badgeRow).toBeVisible({ timeout: 30_000 });

  const anyClaim = (
    await Promise.all(
      INHERITED_CLAIMS.map((c) => badgeRow.getByText(c, { exact: true }).count()),
    )
  ).some((n) => n > 0);
  test.skip(!anyClaim, "no inherited claims are rendered, so there is nothing to qualify");

  await expect(badgeRow.getByTestId("inherited-claims-qualifier")).toBeVisible();
});

test("the landing page does not assert compliance in its own voice", async ({ page }) => {
  // The inherited badges are labelled. Nothing ELSE on the page may state a
  // compliance posture as current Meridian fact -- that is what the handoff
  // document forbids, and a new claim elsewhere would evade the badge test.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

  const body = ((await page.locator("main").textContent()) ?? "").toLowerCase();
  const forbidden = [
    "we are pci compliant",
    "meridian is pci compliant",
    "pci certified",
    "pci-dss certified",
    "sox compliant",
    "fully compliant",
    "certified compliant",
  ];
  for (const phrase of forbidden) {
    expect(body, `the landing page must not assert "${phrase}"`).not.toContain(phrase);
  }
});

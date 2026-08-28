import { test, expect } from "@playwright/test";

/**
 * Wherever the landing page shows an inherited compliance claim, it shows the
 * qualifier right beside it.
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
 * **This spec does not exist to preserve the claims.** It preserves the
 * relationship between a claim and its qualifier. Two properties, and they pull
 * in opposite directions on purpose:
 *
 *   1. Deleting the claims entirely PASSES. That is the sanctioned remediation,
 *      and a guard that failed on it would exist to keep false claims on the
 *      page. An earlier draft of this file asserted the badge row was visible
 *      before checking whether any claim was in it, so removing the row turned
 *      the suite red -- pinning the claims on, which is the outcome the change
 *      argues against. Found in review as ML-CLAIMS-01.
 *   2. A claim reappearing ANYWHERE unqualified FAILS -- not just inside the
 *      row the badges happen to live in today. The earlier draft scoped to
 *      `.badge-row` first, so "PCI compliant" added elsewhere on the page passed
 *      every case. Found in review as ML-CLAIMS-02.
 *
 * Colocation is tested as "the claim's own container also holds the qualifier".
 * A shared ancestor is not enough -- `<body>` contains everything, and a
 * qualifier at the top of the page does not qualify a claim at the bottom.
 */

const INHERITED_CLAIMS = ["SOX-controlled", "PCI compliant", "ECOA / Reg B"];
const QUALIFIER = '[data-testid="inherited-claims-qualifier"]';

type ClaimAudit = {
  qualifierPresent: boolean;
  found: string[];
  unqualified: string[];
};

/** Every rendered occurrence of a claim, and whether each sits with a qualifier. */
async function auditClaims(page: import("@playwright/test").Page): Promise<ClaimAudit> {
  return page.evaluate(
    ({ claims, qualifierSelector }) => {
      const root = document.querySelector("main") ?? document.body;
      const qualifier = root.querySelector(qualifierSelector);
      const found: string[] = [];
      const unqualified: string[] = [];

      for (const el of Array.from(root.querySelectorAll("*"))) {
        // Leaf elements only, so a wrapper is not counted once per claim it
        // happens to contain.
        if (el.children.length > 0) continue;
        const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
        const claim = claims.find((c) => text === c);
        if (!claim) continue;

        found.push(claim);
        // The claim's own container must also hold the qualifier. Walking
        // further up would eventually reach an ancestor of the whole page.
        const container = el.parentElement;
        if (!qualifier || !container || !container.contains(qualifier)) {
          unqualified.push(claim);
        }
      }

      return { qualifierPresent: qualifier !== null, found, unqualified };
    },
    { claims: INHERITED_CLAIMS, qualifierSelector: QUALIFIER },
  );
}

test("no inherited compliance claim is rendered anywhere without a qualifier beside it", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

  const audit = await auditClaims(page);

  if (audit.found.length === 0) {
    // The claims were removed. That is a valid remediation, and this spec
    // deliberately does not force them back onto the page.
    return;
  }

  expect(
    audit.unqualified,
    `these inherited claims are rendered with no qualifier in their own container: ` +
      `${audit.unqualified.join(", ")}. An unsupported claim must never be presented ` +
      `as Meridian's own -- see docs/DEBT.md D25.`,
  ).toEqual([]);
});

test("the qualifier says inherited and unverified, whatever wording it uses", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

  const audit = await auditClaims(page);
  test.skip(audit.found.length === 0, "no inherited claims are rendered, so nothing to qualify");

  const qualifier = page.locator(QUALIFIER).first();
  await expect(qualifier).toBeVisible();

  // The wording may change to fit the UI; the meaning may not.
  const text = ((await qualifier.textContent()) ?? "").toLowerCase();
  expect(text).toContain("inherited");
  expect(
    /not verified|unverified/.test(text),
    "the qualifier must say the claims are not verified, not merely that they are old",
  ).toBe(true);
});

test("the landing page does not assert compliance in its own voice", async ({ page }) => {
  // The inherited badges are labelled. Nothing ELSE may state a compliance
  // posture as current Meridian fact -- a new sentence would evade the
  // claim-string check above by not matching one of the three exactly.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

  const body = ((await page.locator("main").innerText()) ?? "")
    .toLowerCase()
    .replace(/\s+/g, " ");
  const forbidden = [
    "we are pci compliant",
    "meridian is pci compliant",
    "pci certified",
    "pci-dss certified",
    "pci dss certified",
    "sox compliant",
    "sox certified",
    "ecoa compliant",
    "reg b compliant",
    "fully compliant",
    "certified compliant",
    "compliance certified",
  ];
  for (const phrase of forbidden) {
    expect(body, `the landing page must not assert "${phrase}"`).not.toContain(phrase);
  }
});

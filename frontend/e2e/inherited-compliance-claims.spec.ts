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
 *      row the badges happen to live in today, and not only as a standalone
 *      element. An earlier draft scoped to `.badge-row` first, so "PCI
 *      compliant" added elsewhere passed every case; the draft after that
 *      matched leaf text exactly, so `<p>Our payment flows are PCI
 *      compliant.</p>` still passed. Found in review as ML-CLAIMS-02, twice.
 *
 * Matching is case- and spacing-insensitive. A claim does not stop being the
 * claim when it is typed differently: "pci compliant" and "ECOA/Reg B" assert
 * exactly what the badges do, and a case-sensitive guard let both back onto the
 * page with the suite green (ML-CLAIMS-02 residual, round 3).
 *
 * Note what property 2 costs: prose that MENTIONS a claim in order to deny it
 * needs the qualifier in its container too. That is deliberate. A reader
 * scanning the page sees the claim either way, and the alternative -- guessing
 * at negation near the string -- is the kind of cleverness that fails quietly.
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

/** Every rendered occurrence of a claim, and whether each sits with a qualifier.
 *
 * Occurrences are found by SUBSTRING, on the smallest element that contains the
 * claim. Exact leaf matching was not enough: it collected
 * `<span>PCI compliant</span>` and missed `<p>Our payment flows are PCI
 * compliant.</p>`, so the string the handoff document forbids could return in
 * ordinary copy with the suite green (review finding ML-CLAIMS-02). Taking the
 * smallest containing element also survives a claim split across inline tags --
 * `PCI <b>compliant</b>` has no leaf holding the whole string, but the `<p>`
 * around it does.
 */
async function auditClaims(page: import("@playwright/test").Page): Promise<ClaimAudit> {
  return page.evaluate(
    ({ claims, qualifierSelector }) => {
      const root = document.querySelector("main") ?? document.body;
      const qualifier = root.querySelector(qualifierSelector);
      // Case- and spacing-insensitive, because a claim does not stop being the
      // claim when it is typed differently. "pci compliant" and "ECOA/Reg B"
      // are the same assertions as the badge text, and a case-sensitive guard
      // let both back onto the page with the suite green.
      const normalise = (s: string | null) =>
        (s ?? "")
          .toLowerCase()
          .replace(/\s*\/\s*/g, "/")
          .replace(/\s+/g, " ")
          .trim();
      const found: string[] = [];
      const unqualified: string[] = [];

      for (const claim of claims) {
        for (const el of Array.from(root.querySelectorAll("*"))) {
          const needle = normalise(claim);
          if (!normalise(el.textContent).includes(needle)) continue;
          // Smallest element containing it: if a child also contains the claim,
          // this one is a wrapper and the child is the real occurrence.
          const childHasIt = Array.from(el.children).some((c) =>
            normalise(c.textContent).includes(needle),
          );
          if (childHasIt) continue;

          found.push(claim);
          // The claim and the qualifier must be SIBLINGS -- same immediate
          // container -- or the claim's element must contain the qualifier
          // outright.
          //
          // "An ancestor contains both" is far too loose: a <p> elsewhere in the
          // same <section> as the badge row passes that test while reading, to
          // anyone looking at the page, as an unqualified claim. That is how the
          // embedded-prose case slipped through a first attempt at this fix.
          // Sibling is what "beside it" actually means.
          const holdsQualifier =
            qualifier !== null &&
            (el.contains(qualifier) || el.parentElement === qualifier.parentElement);
          if (!holdsQualifier) unqualified.push(claim);
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

import { test, expect } from "@playwright/test";
import { fictionalApplicant, currentAppId, dbClient } from "./fixtures";

/**
 * Week 1-4 client review: "The trainer could not correct anything from the
 * review screen. Done when: a user can go back and change an answer without
 * restarting."
 *
 * Back did already exist on Step 4 and did preserve answers, so nothing was
 * literally lost -- but correcting a Step 1 field meant three Back presses and
 * then three Next presses to return, with no indication from the review screen
 * that any of it was possible. In practice that is not an edit affordance, and
 * the trainer's experience is the evidence.
 *
 * These tests drive the corrected journey the way a person would: reach the
 * review, notice a wrong answer, fix it, and submit -- checking that the value
 * that reaches Postgres is the corrected one, not the original.
 */

/** Fill steps 1-3 and stop on the review screen. */
async function fillToReview(page, applicant, overrides: { employer?: string } = {}) {
  await page.goto("/apply");
  await expect(page.getByText("Step 1 of 5")).toBeVisible();
  await page.getByPlaceholder("Jane Q. Borrower").fill(applicant.name);
  await page.locator('input[type="date"]').fill("1990-01-01");
  await page.getByPlaceholder("123-45-6789").fill(applicant.ssn);
  await page.getByPlaceholder("you@example.com").fill(applicant.email);
  await page.getByPlaceholder("(555) 555-0123").fill(applicant.phone);
  await page.getByPlaceholder("123 Main St").fill("1 Fictional Ave");
  await page.getByPlaceholder("Springfield").fill("Springfield");
  await page.locator("select").first().selectOption("IL");
  await page.getByPlaceholder("62704").fill("62704");
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 2 of 5")).toBeVisible();
  const plain = page.locator('main input:visible:not([placeholder]):not([type="range"])');
  await plain.nth(0).fill(overrides.employer ?? "Wrong Employer Co");
  await plain.nth(1).fill("QA Analyst");
  await page.getByPlaceholder("65000").fill(String(applicant.income));
  await page.getByPlaceholder("3").fill("3");
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 3 of 5")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 4 of 5")).toBeVisible();
}

test("a wrong answer can be corrected from the review screen and the correction is what gets submitted", async ({ page }) => {
  const applicant = fictionalApplicant("Alex", /* even ssn */ true, 90_000);
  await fillToReview(page, applicant);

  // The review shows the wrong value the applicant typed.
  await expect(page.getByText("Wrong Employer Co")).toBeVisible();

  // The affordance the trainer could not find: an Edit control per section,
  // named so it is unambiguous with a screen reader.
  await page.getByRole("button", { name: "Edit Employment & income" }).click();

  // Lands on the step that owns the field, with the answers still in place --
  // not a restarted wizard.
  await expect(page.getByText("Step 2 of 5")).toBeVisible();
  const plain = page.locator('main input:visible:not([placeholder]):not([type="range"])');
  await expect(plain.nth(0)).toHaveValue("Wrong Employer Co");
  await expect(page.getByPlaceholder("65000")).toHaveValue(String(applicant.income));

  await plain.nth(0).fill("Corrected Employer Ltd");

  // One click back to the review -- not Next, Next, Next.
  await page.getByRole("button", { name: "Save and return to review" }).click();
  await expect(page.getByText("Step 4 of 5")).toBeVisible();
  await expect(page.getByText("Corrected Employer Ltd")).toBeVisible();
  await expect(page.getByText("Wrong Employer Co")).not.toBeVisible();

  // The correction is what is actually persisted.
  await page.getByRole("button", { name: "Submit application" }).click();
  await expect(page.getByText("Step 5 of 5")).toBeVisible({ timeout: 15_000 });
  const appId = await currentAppId(page);

  const client = dbClient();
  await client.connect();
  try {
    const row = await client.query("SELECT employer FROM applications WHERE id = $1", [appId]);
    expect(row.rows[0].employer).toBe("Corrected Employer Ltd");
  } finally {
    await client.end();
  }
});

test("editing a Step 1 field from the review takes one click each way", async ({ page }) => {
  /** The specific case that was three clicks out and three back. */
  const applicant = fictionalApplicant("Sam", true, 90_000);
  await fillToReview(page, applicant);

  await page.getByRole("button", { name: "Edit Personal" }).click();
  await expect(page.getByText("Step 1 of 5")).toBeVisible();

  const corrected = "Corrected Name";
  await page.getByPlaceholder("Jane Q. Borrower").fill(corrected);
  await page.getByRole("button", { name: "Save and return to review" }).click();

  await expect(page.getByText("Step 4 of 5")).toBeVisible();
  await expect(page.getByText(corrected)).toBeVisible();
});

test("an edit that breaks validation cannot be returned to the review", async ({ page }) => {
  /** The correction path must not become a way around the wizard's own rules:
   * an emptied required field would otherwise arrive at review looking
   * complete. */
  const applicant = fictionalApplicant("Jordan", true, 90_000);
  await fillToReview(page, applicant);

  await page.getByRole("button", { name: "Edit Personal" }).click();
  await expect(page.getByText("Step 1 of 5")).toBeVisible();

  await page.getByPlaceholder("you@example.com").fill("not-an-email");
  await page.getByRole("button", { name: "Save and return to review" }).click();

  // Held on the step with the error shown, not returned to review.
  await expect(page.getByText("Step 1 of 5")).toBeVisible();
  await expect(page.getByText("Enter a valid email")).toBeVisible();

  // Fixing it lets the return through.
  await page.getByPlaceholder("you@example.com").fill(applicant.email);
  await page.getByRole("button", { name: "Save and return to review" }).click();
  await expect(page.getByText("Step 4 of 5")).toBeVisible();
});

test("the normal forward path is unchanged by the edit affordance", async ({ page }) => {
  /** Regression guard: someone who never uses Edit must see the ordinary
   * Next button, not the return-to-review one. */
  const applicant = fictionalApplicant("Riley", true, 90_000);
  await page.goto("/apply");
  await expect(page.getByText("Step 1 of 5")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save and return to review" }),
  ).not.toBeVisible();

  await fillToReview(page, applicant, { employer: "Fictional Testing Co" });
  // Reached review the ordinary way; Edit controls are present for all three.
  await expect(page.getByRole("button", { name: "Edit Personal" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit Employment & income" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit Loan details" })).toBeVisible();
});

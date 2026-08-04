import { Page, expect } from "@playwright/test";
import { Client } from "pg";

/** Fictional test data only -- no real/production-like SSNs or card data.
 * The last SSN digit is fixed (even -> approve band, odd -> deny/refer
 * band, matching decision-service's stub scoring: bureau_score = 680 if
 * even else 612) while the rest of the number is derived from the current
 * timestamp so repeated runs never collide or depend on a prior run's
 * application id. */
export function uniqueDigits(len: number): string {
  return Date.now().toString().slice(-len).padStart(len, "0");
}

export interface FictionalApplicant {
  name: string;
  ssn: string;
  email: string;
  phone: string;
  income: number;
}

export function fictionalApplicant(label: string, lastDigitEven: boolean, income: number): FictionalApplicant {
  const d = uniqueDigits(9);
  const lastDigit = lastDigitEven ? "0" : "1";
  const ssn = `999-${d.slice(0, 2)}-${d.slice(2, 5)}${lastDigit}`;
  const phone = `(555) 0${d.slice(5, 7)}-${d.slice(2, 6)}`;
  return {
    name: `${label} Fictional`,
    ssn,
    email: `${label.toLowerCase()}.${d}@example.test`,
    phone,
    income,
  };
}

export async function submitApplication(page: Page, applicant: FictionalApplicant): Promise<void> {
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
  const plainInputs = page.locator('main input:visible:not([placeholder]):not([type="range"])');
  await plainInputs.nth(0).fill("Fictional Testing Co");
  await plainInputs.nth(1).fill("QA Analyst");
  await page.getByPlaceholder("65000").fill(String(applicant.income));
  await page.getByPlaceholder("3").fill("3");
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 3 of 5")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 4 of 5")).toBeVisible();
  await page.getByRole("button", { name: "Submit application" }).click();

  await expect(page.getByText("Step 5 of 5")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("received")).toBeVisible({ timeout: 15_000 });
}

export async function currentAppId(page: Page): Promise<string> {
  const text = await page.locator(".alert-info").innerText();
  const m = text.match(/#(\d+)/);
  if (!m) throw new Error(`could not find an application id in: ${text}`);
  return m[1];
}

export async function getDecision(page: Page): Promise<void> {
  await page.getByRole("button", { name: /Get decision/ }).click();
  await expect(page.getByText("Underwriting decision")).toBeVisible({ timeout: 15_000 });
}

/** Postgres, read-only, used only to verify test invariants (never used by
 * application runtime code). Connects using the same DATABASE_URL the
 * backend services use -- required, not defaulted to a real credential. */
export function dbClient(): Client {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is required to run these E2E tests (see e2e/README.md)");
  }
  return new Client({ connectionString: url });
}

export async function countRows(client: Client, table: string, whereCol: string, appId: string): Promise<number> {
  const res = await client.query(`SELECT count(*)::int AS n FROM ${table} WHERE ${whereCol} = $1`, [appId]);
  return res.rows[0].n;
}

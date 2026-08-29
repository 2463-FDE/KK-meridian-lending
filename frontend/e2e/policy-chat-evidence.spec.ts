import { test, expect } from "@playwright/test";
import { signInAsStaff } from "./fixtures";

/**
 * Policy Chat shows the evidence behind an answer, and says when there is none.
 *
 * **The answers here are stubbed at the network boundary, deliberately.** The
 * agent, the retrieval and the refusal contract are unchanged by this work and
 * are owned by `services/loan-assistant/tests/`. What this file tests is the
 * part a reader sees, and that part must be provable without a live model:
 * policy chat needs Bedrock credentials to answer at all, so a browser test
 * that asked a real question would fail in CI for want of a credential rather
 * than for want of a working panel.
 *
 * The first version of this spec did exactly that -- clicked the chips and
 * waited for real answers -- and it could not have passed anywhere but a
 * machine with credentials. Recorded because the failure looks like a flaky
 * test rather than a badly scoped one.
 *
 * What IS asserted, and what makes it worth having:
 *
 *   * a grounded answer is labelled grounded, names its document in readable
 *     form, and can show the excerpt on request;
 *   * a REFUSAL gets none of that dressing. A panel that showed "Grounded in
 *     policy" beside an answer with no excerpt behind it would be making the
 *     exact claim the refusal path exists to prevent, and would look right
 *     while doing it;
 *   * an `answerable` response that arrives with NO excerpt still gets no
 *     badge -- the badge follows the evidence, not the flag.
 *
 * Whether the shipped example chips can actually be answered from the corpus is
 * a question about the corpus rather than the panel, and is asserted without a
 * model in
 * `services/loan-assistant/tests/test_policy_chat_examples_are_answerable.py`.
 */

const GROUNDED = {
  answerable: true,
  answer: "The late fee is the lesser of $35.00 and 5% of the unpaid scheduled principal and interest for that installment.",
  source_chunk_id: "fee_schedule.md#2.1",
  source_text:
    "Late payment fee | Decided 2026-08-29. At most one fee per missed scheduled installment.",
};

const REFUSAL = {
  answerable: false,
  answer:
    "I can only answer from Meridian's lending policy documents, and I could not find anything there that answers this.",
  source_chunk_id: null,
  source_text: null,
};

/** `answerable` with nothing behind it. The badge must still not appear. */
const GROUNDLESS = {
  answerable: true,
  answer: "Something that claims to be an answer.",
  source_chunk_id: null,
  source_text: null,
};

async function openPolicyChat(
  page: import("@playwright/test").Page,
  reply: unknown,
) {
  // Staff, because the PAGE is staff-gated even though the gateway route is
  // anonymous. That mismatch is deliberate and recorded in docs/DEBT.md RF-28;
  // this spec follows current behaviour rather than asserting an audience.
  await signInAsStaff(page, "underwriter");
  await page.route("**/assistant/policy-chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(reply),
    });
  });
  await page.goto("/policy-chat");
  await expect(page.getByRole("heading", { name: /policy chat/i })).toBeVisible({
    timeout: 20_000,
  });
}

//: Mirrors PolicyChat.tsx. The third chip is "standard loan terms" rather than
// "loan terms are available" because the answerability gate rejects the latter
// -- see `test_policy_chat_examples_are_answerable.py`, which reads the chips
// out of the component and runs that gate, so the two cannot drift.
const CHIPS = [
  "What is the late fee?",
  "What score requires manual review?",
  "What are the standard loan terms?",
];

test("the example chips are offered before anything has been asked", async ({ page }) => {
  await openPolicyChat(page, GROUNDED);

  const examples = page.getByTestId("policy-chat-examples");
  await expect(examples).toBeVisible();
  for (const label of CHIPS) {
    await expect(examples.getByRole("button", { name: label })).toBeVisible();
  }
});

test("a grounded answer names its source and can show the excerpt", async ({ page }) => {
  await openPolicyChat(page, GROUNDED);

  await page
    .getByTestId("policy-chat-examples")
    .getByRole("button", { name: CHIPS[0] })
    .click();
  await expect(page.getByTestId("policy-turn-0")).toBeVisible({ timeout: 20_000 });

  await expect(page.getByTestId("policy-grounded-0")).toBeVisible();

  // A friendly document name, not the raw retrieval pointer.
  const source = page.getByTestId("policy-source-0");
  await expect(source).toHaveText("Fee Schedule");

  // The excerpt is proof, and it is collapsed until asked for.
  await expect(page.getByTestId("policy-evidence-0")).toHaveCount(0);
  const toggle = page.getByTestId("policy-evidence-toggle-0");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();

  await expect(page.getByTestId("policy-evidence-0")).toContainText(
    "At most one fee per missed scheduled installment",
  );
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  // The raw chunk id is available inside the evidence, secondary rather than on
  // the answer line.
  await expect(page.getByTestId("policy-chunk-0")).toHaveText("fee_schedule.md#2.1");
});

test("a refusal gets no grounded badge and no evidence", async ({ page }) => {
  await openPolicyChat(page, REFUSAL);

  await page.getByRole("textbox").fill("What is the share price of an unrelated company?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page.getByTestId("policy-refusal-0")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("policy-grounded-0")).toHaveCount(0);
  await expect(page.getByTestId("policy-evidence-toggle-0")).toHaveCount(0);
  await expect(page.getByTestId("policy-source-0")).toHaveCount(0);
});

test("an answerable reply with no excerpt is still not labelled grounded", async ({
  page,
}) => {
  // The badge follows the EVIDENCE, not the flag. If it followed `answerable`
  // alone, a reply that lost its excerpt would still claim to be grounded --
  // which is the one thing this panel must never do, and it would look correct.
  await openPolicyChat(page, GROUNDLESS);

  await page.getByRole("textbox").fill("Anything at all");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page.getByTestId("policy-turn-0")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("policy-grounded-0")).toHaveCount(0);
  await expect(page.getByTestId("policy-evidence-toggle-0")).toHaveCount(0);
});

test("Clear this session empties the transcript in the browser", async ({ page }) => {
  await openPolicyChat(page, GROUNDED);

  await page
    .getByTestId("policy-chat-examples")
    .getByRole("button", { name: CHIPS[2] })
    .click();
  await expect(page.getByTestId("policy-turn-0")).toBeVisible({ timeout: 20_000 });

  await page.getByTestId("policy-clear-session").click();

  await expect(page.getByTestId("policy-chat-turns")).toHaveCount(0);
  // The chips come back, which is what makes it a cleared panel rather than an
  // emptied one.
  await expect(page.getByTestId("policy-chat-examples")).toBeVisible();
});

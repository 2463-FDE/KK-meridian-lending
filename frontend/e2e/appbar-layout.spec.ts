import { test, expect } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";
import { signInAsStaff } from "./fixtures";

/**
 * The application header holds together at the widths this gets presented at.
 *
 * The measured problem, before this: `.appbar-inner` carried the page BODY's
 * `max-width: 1080px` while the header contains a wordmark, up to seven
 * navigation destinations and a user block. At a 1440px viewport the bar rendered
 * 1080px wide with 180px unused on each side, and inside it the wordmark wrapped
 * to two lines ("Meridian / Lending"), "Policy Chat" wrapped, and "Log out"
 * wrapped -- inside a 60px-tall bar. At 768px the document overflowed
 * horizontally.
 *
 * **These assertions are geometric and semantic, never CSS values.** A test that
 * asserted `max-width: 1400px` would fail the next time someone improves the
 * spacing and would not have caught the original bug, which was not any single
 * value but a container measured as if it were the page body. What matters is:
 * does a label occupy one line, do the groups avoid each other, does the document
 * stay inside the viewport.
 *
 * **Line counting is done with a Range, not by dividing height by line-height.**
 * The naive version says "Policy Chat" is two lines when its box is 33px tall
 * with 14px of vertical padding -- padding is not text. `Range.getClientRects()`
 * measures the text itself; see `textLineCount` for why its rects still have to
 * be clustered rather than counted.
 *
 * **Overlap is checked in both axes.** Comparing only x-extents reports an
 * overlap for the deliberate two-row layout at narrow widths, where the nav sits
 * BELOW the user block rather than through it.
 */

test.describe.configure({ timeout: 120_000 });

/**
 * How many visual lines this element's text occupies.
 *
 * `Range.getClientRects()` returns one rect per inline box, not per line, so a
 * single line built from several spans yields several rects -- and they do NOT
 * share a top edge. The wordmark is a case in point: the diamond is 18px and
 * nudged half a pixel by a transform while the text beside it is 17px, so an
 * exact-top comparison counts one visual line as two.
 *
 * So the rects are CLUSTERED into bands with a tolerance proportional to the
 * tallest rect. Two rects belong to the same line when their tops are closer
 * together than most of a line's height; a genuine second line is a whole
 * line-height away and lands in its own band.
 */
async function textLineCount(locator: Locator): Promise<number> {
  return locator.first().evaluate((el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const rects = Array.from(range.getClientRects()).filter(
      (r) => r.width > 0 && r.height > 0,
    );
    if (rects.length === 0) return 1;

    const tallest = Math.max(...rects.map((r) => r.height));
    const tolerance = tallest * 0.6;

    const bands: number[] = [];
    for (const rect of rects.slice().sort((a, b) => a.top - b.top)) {
      const existing = bands.find((top) => Math.abs(top - rect.top) <= tolerance);
      if (existing === undefined) bands.push(rect.top);
    }
    return bands.length;
  });
}

/** True when two elements' boxes intersect in BOTH axes. */
async function overlaps(a: Locator, b: Locator): Promise<boolean> {
  const [ra, rb] = [await a.first().boundingBox(), await b.first().boundingBox()];
  if (!ra || !rb) throw new Error("an element being compared was not rendered");
  const gap = 1; // a shared 1px border is not an overlap
  const xOverlap = ra.x + ra.width - gap > rb.x && rb.x + rb.width - gap > ra.x;
  const yOverlap = ra.y + ra.height - gap > rb.y && rb.y + rb.height - gap > ra.y;
  return xOverlap && yOverlap;
}

async function documentOverflowsHorizontally(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
}

const policyChat = (page: Page) =>
  page.locator(".nav-link", { hasText: "Policy Chat" });
const logout = (page: Page) => page.getByRole("button", { name: /Log out/ });

/** Desktop presentation widths. 1366x768 is the one in the brief. */
const DESKTOP = [
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1280, height: 720 },
];

for (const size of DESKTOP) {
  test(`admin header holds one row at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    await signInAsStaff(page, "admin");
    await page.goto("/admin");

    // Admin carries the widest nav: seven destinations.
    await expect(page.locator(".nav-link")).toHaveCount(7);

    // Each of the three things the brief names as broken.
    expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
    expect(await textLineCount(policyChat(page))).toBe(1);
    expect(await textLineCount(logout(page))).toBe(1);

    // The identity is two deliberate lines: the name as the session gives it,
    // then the role. Not three, and not a name broken mid-string.
    expect(await textLineCount(page.locator(".auth-name"))).toBe(1);

    // One row: the nav and the user block sit side by side, not through each
    // other and not stacked.
    expect(await overlaps(page.locator(".appbar-nav"), page.locator(".appbar-auth")))
      .toBe(false);
    const nav = await page.locator(".appbar-nav").boundingBox();
    const auth = await page.locator(".appbar-auth").boundingBox();
    expect(nav && auth && Math.abs(nav.y - auth.y)).toBeLessThan(20);

    // Nothing hangs off the side of the page.
    expect(await documentOverflowsHorizontally(page)).toBe(false);
    const inner = await page.locator(".appbar-inner").boundingBox();
    expect(inner!.x).toBeGreaterThanOrEqual(0);
    expect(inner!.x + inner!.width).toBeLessThanOrEqual(size.width + 1);
  });
}

/**
 * The two boundary widths, asserted on both sides of the breakpoint.
 *
 * Review finding APPBAR-BP-GAP asked whether a band existed between the
 * tightened-spacing rule and the two-row rule where neither helped and the
 * header broke. Measured: it does not. Swept every 10px from 1020 to 1220 and
 * every 1-2px across 1039-1052, the header holds one row down to 1041px with
 * 14px still between the nav and the user block, and the two-row layout takes
 * over at 1040px.
 *
 * The finding was right about the coverage though: the suite tested 1024 and
 * 1280+ and never the boundary, so nothing would have noticed if a spacing
 * change moved the real limit above the breakpoint and opened the band for
 * real. These two tests are that guard.
 */
test("one row still fits at 1041px, the width just above the breakpoint", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1041, height: 800 });
  await signInAsStaff(page, "admin");
  await page.goto("/admin");

  await expect(page.locator(".nav-link")).toHaveCount(7);
  expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
  expect(await textLineCount(policyChat(page))).toBe(1);
  expect(await textLineCount(logout(page))).toBe(1);
  expect(await overlaps(page.locator(".appbar-nav"), page.locator(".appbar-auth")))
    .toBe(false);
  expect(await documentOverflowsHorizontally(page)).toBe(false);

  // Still ONE row here: the nav and the user block share a baseline. If a future
  // spacing change pushes the real limit above 1041, this is what fails -- which
  // is the band the review asked about.
  const nav = await page.locator(".appbar-nav").boundingBox();
  const auth = await page.locator(".appbar-auth").boundingBox();
  expect(Math.abs(nav!.y - auth!.y)).toBeLessThan(20);

  // And there is margin, not a hairline. A clearance that has silently gone to
  // 1px is a layout about to break on the next label added.
  expect(auth!.x - (nav!.x + nav!.width)).toBeGreaterThan(4);
});

test("1040px is where the second row deliberately begins", async ({ page }) => {
  await page.setViewportSize({ width: 1040, height: 800 });
  await signInAsStaff(page, "admin");
  await page.goto("/admin");

  // Two rows now -- by decision, not by accident. Asserted so the breakpoint
  // cannot drift away from the width the rule names without a test saying so.
  const nav = await page.locator(".appbar-nav").boundingBox();
  const auth = await page.locator(".appbar-auth").boundingBox();
  expect(Math.abs(nav!.y - auth!.y)).toBeGreaterThan(20);

  // Everything that mattered on one row still holds on two.
  await expect(page.locator(".nav-link")).toHaveCount(7);
  expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
  expect(await textLineCount(policyChat(page))).toBe(1);
  expect(await textLineCount(logout(page))).toBe(1);
  await expect(logout(page)).toBeVisible();
  expect(await documentOverflowsHorizontally(page)).toBe(false);
});

test("a narrower viewport rearranges deliberately instead of wrapping words", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await signInAsStaff(page, "admin");
  await page.goto("/admin");

  // The nav is allowed to move to its own row here. What is NOT allowed is a
  // label breaking mid-phrase, which is the difference between a layout decision
  // and a layout accident.
  expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
  expect(await textLineCount(policyChat(page))).toBe(1);
  expect(await textLineCount(logout(page))).toBe(1);

  // Every destination is still present and reachable -- no hidden nav with no
  // replacement.
  await expect(page.locator(".nav-link")).toHaveCount(7);
  await expect(logout(page)).toBeVisible();

  expect(await documentOverflowsHorizontally(page)).toBe(false);
});

test("a tablet viewport keeps every destination and the logout button", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await signInAsStaff(page, "admin");
  await page.goto("/admin");

  expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
  expect(await textLineCount(logout(page))).toBe(1);
  await expect(page.locator(".nav-link")).toHaveCount(7);
  await expect(logout(page)).toBeVisible();

  // This is the width that used to overflow the document horizontally.
  expect(await documentOverflowsHorizontally(page)).toBe(false);
});

test("a different role's nav set also holds one row", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await signInAsStaff(page, "csr");
  await page.goto("/servicing");

  // csr has six destinations, not seven -- fixing the layout for admin must not
  // depend on admin's exact item count.
  await expect(page.locator(".nav-link")).toHaveCount(6);
  expect(await textLineCount(page.locator(".wordmark"))).toBe(1);
  expect(await textLineCount(policyChat(page))).toBe(1);
  expect(await textLineCount(logout(page))).toBe(1);
  expect(await overlaps(page.locator(".appbar-nav"), page.locator(".appbar-auth")))
    .toBe(false);
  expect(await documentOverflowsHorizontally(page)).toBe(false);
});

test("the active destination is marked, and navigation is a named landmark", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await signInAsStaff(page, "admin");
  await page.goto("/policy-chat");

  // Active state is what tells an operator where they are; asserted by class
  // because that is what the component sets, and by the element identity so it
  // cannot pass on a different link being active.
  await expect(policyChat(page)).toHaveClass(/nav-link-active/);

  // Semantics: a header, one named nav landmark, real link text and a real
  // button. `aria-label` because a page can carry more than one nav.
  await expect(page.getByRole("banner")).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Primary navigation" }),
  ).toBeVisible();
  await expect(logout(page)).toHaveText(/Log out/);
});

test("keyboard focus on a header control is visibly marked", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await signInAsStaff(page, "admin");
  await page.goto("/admin");

  // What this does and does not prove, stated plainly. `globals.css` had no
  // `:focus-visible` rule for links or buttons, and this test PASSES without one
  // -- Chromium paints its own focus ring, so the header was never actually
  // missing a focus indicator. Confirmed by running this file against the
  // pre-change stylesheet: the six layout tests failed and this one passed.
  //
  // The header rule added alongside this test therefore makes the ring explicit
  // and consistent with the palette rather than fixing an invisible state. The
  // test earns its place as a guard against the thing that WOULD break it: an
  // `outline: none` added later for tidiness, which is a common and quiet way to
  // remove keyboard affordance.
  const outline = await policyChat(page).first().evaluate((el) => {
    (el as HTMLElement).focus();
    const cs = getComputedStyle(el);
    return { width: cs.outlineWidth, style: cs.outlineStyle };
  });

  expect(outline.style).not.toBe("none");
  expect(parseFloat(outline.width)).toBeGreaterThan(0);
});

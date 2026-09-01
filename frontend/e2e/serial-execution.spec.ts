import { readFileSync } from "node:fs";
import { join } from "node:path";

import { test, expect } from "@playwright/test";

/**
 * This suite is only a gate because it runs serially, so that is guarded.
 *
 * RF-24 recorded the browser suite as unusable as a pass/fail gate: a full run
 * failed specs that passed alone, a different set each time. Re-measured on
 * 2026-09-01 with `docker compose down -v` before every run, the picture is
 * sharper than that:
 *
 *   workers=1   216/216 passed, 191s   -- and again 216/216, 220s
 *   workers=2   213/216 passed, 183s
 *   workers=4   195/216 passed, 443s   -- and again  84/216, 553s
 *
 * Two things follow. The flakiness is real and reproduces at four workers, with
 * a different failure set each time. And it is CONTENTION, not test
 * independence: wall-clock time rises with worker count, so parallelism makes
 * the suite slower as well as less reliable. Sampling container CPU during a
 * parallel run puts a number on it -- `origination-service` peaks at 95% while
 * the frontend, gateway, Postgres and servicing-service all stay under 10%.
 *
 * The earlier measurements in RF-24 were taken at `--workers=4`, which is not
 * what this project configures. `playwright.config.ts` sets `workers: 1` and
 * `fullyParallel: false`, and CI runs `npm run test:e2e` with no override.
 *
 * SO THE GATE PROPERTY RESTS ENTIRELY ON THAT CONFIGURATION, and `workers: 1`
 * is exactly the line somebody speeds up later without re-taking these
 * measurements. This file is the thing that makes that a deliberate decision:
 * raising the worker count fails here, with the numbers attached, so whoever
 * does it has to argue with the data rather than discover it three PRs later.
 *
 * It is not a claim that the specs are independent. They are not -- per-spec
 * database isolation does not exist, three specs share a seeded loan while one
 * mutates it, and two consume a reserved loan per run (RF-27). Those are why
 * the suite must stay serial, not reasons to think it could go parallel safely.
 */

const CONFIG = readFileSync(join(__dirname, "..", "playwright.config.ts"), "utf-8");

test.describe("the browser suite runs serially, and that is load-bearing", () => {
  test("playwright.config.ts pins one worker", () => {
    // Matched on the setting rather than the file's whole text: a comment
    // mentioning workers must not satisfy this, and a reformat must not break
    // it.
    const workers = CONFIG.match(/^\s*workers:\s*([^,\n]+)/m);
    expect(
      workers,
      "playwright.config.ts no longer sets `workers` at all, so the suite runs " +
        "at Playwright's default (half the CPU count). Measured at four " +
        "workers this suite fails 21 and then 66 of 216, and takes longer than " +
        "running serially.",
    ).not.toBeNull();
    expect(
      workers?.[1].trim(),
      "the worker count moved off 1. At 2 workers the suite failed 3 of 216; " +
        "at 4 it failed 21, then 66, and wall-clock time rose from 191s to " +
        "553s. If this is being raised deliberately, re-take the measurements " +
        "in docs/DEBT.md RF-24 first -- and note that per-spec database " +
        "isolation still does not exist.",
    ).toBe("1");
  });

  test("fullyParallel is off", () => {
    const parallel = CONFIG.match(/^\s*fullyParallel:\s*([^,\n]+)/m);
    expect(parallel?.[1].trim()).toBe("false");
  });

  test("no retry masks a real failure", () => {
    /**
     * `retries: 0` is part of the same property. A retry would turn the
     * contention failures above into green runs with a slower clock, which is
     * worse than failing: the suite would stop being able to tell anybody that
     * something is wrong, and RF-24's original symptom would come back as
     * "sometimes CI is slow".
     */
    const retries = CONFIG.match(/^\s*retries:\s*([^,\n]+)/m);
    expect(
      retries?.[1].trim(),
      "retries are enabled. A retry hides exactly the contention failures RF-24 " +
        "measured, and a suite that retries into green cannot be a gate.",
    ).toBe("0");
  });
});

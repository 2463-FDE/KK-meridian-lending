import { test, expect } from "@playwright/test";
import { fictionalApplicant, uniqueDigits } from "./fixtures";

/**
 * The test-data namespace is unique per worker process, not just per call.
 *
 * **RF-24, the part of it that is provable without a browser.** `uniqueDigits`
 * builds the identifiers every application-creating spec uses, and
 * `fictionalApplicant` cuts the SSN out of characters 0-4 of the result. Its
 * counter is module scope, so it starts at `0` in every Playwright worker: under
 * `workers: 1` that is genuinely collision-free, but with four workers four
 * processes each begin at the same counter and uniqueness falls back to the six
 * timestamp digits differing — timing luck, not a guarantee. Two workers calling
 * it in the same millisecond window mint the same SSN, and the second
 * application's submission fails in a spec that has nothing to do with the cause.
 *
 * These tests use no browser and no database on purpose. The mechanism is
 * arithmetic on a string, so it can be asserted directly instead of waited for:
 * a race reproduces on someone else's machine at some other time, and that is
 * exactly the kind of evidence that gets called flaky and ignored.
 */

test.describe("fixture namespace", () => {
  test("the worker index reaches the digits the SSN is cut from", async () => {
    const original = process.env.TEST_WORKER_INDEX;
    try {
      const leadingFor = (workerIndex: string) => {
        process.env.TEST_WORKER_INDEX = workerIndex;
        // Several calls, so a single lucky value cannot pass this.
        return new Set(
          Array.from({ length: 12 }, () => uniqueDigits(9).charAt(0)),
        );
      };

      const workerZero = leadingFor("0");
      const workerThree = leadingFor("3");

      // Disjoint: nothing minted by worker 0 can be confused with worker 3.
      expect([...workerZero]).toEqual(["0"]);
      expect([...workerThree]).toEqual(["3"]);
    } finally {
      if (original === undefined) delete process.env.TEST_WORKER_INDEX;
      else process.env.TEST_WORKER_INDEX = original;
    }
  });

  test("no SSN minted by one worker can be minted by another", async () => {
    const original = process.env.TEST_WORKER_INDEX;
    try {
      // Written this way because the obvious version does not test what it
      // says. Minting one applicant per worker and asserting the two SSNs
      // differ passes WITHOUT the worker index at all -- the module counter
      // advances between the two calls, so they differ for an unrelated reason.
      // Confirmed by removing the worker index and watching that version stay
      // green.
      //
      // The real property is that the two workers draw from disjoint RANGES, so
      // this collects a set per worker and asserts they cannot intersect.
      const ssnsFor = (workerIndex: string) => {
        process.env.TEST_WORKER_INDEX = workerIndex;
        return new Set(
          Array.from({ length: 15 }, () =>
            fictionalApplicant("w" + workerIndex, true, 90_000).ssn),
        );
      };

      const fromZero = ssnsFor("0");
      const fromThree = ssnsFor("3");

      const shared = [...fromZero].filter((ssn) => fromThree.has(ssn));
      expect(shared).toEqual([]);

      // And the ranges are separated by construction, not by luck: the worker
      // digit is the first character of the SSN's middle group.
      expect([...fromZero].every((s) => s.split("-")[1].startsWith("0"))).toBe(true);
      expect([...fromThree].every((s) => s.split("-")[1].startsWith("3"))).toBe(true);
    } finally {
      if (original === undefined) delete process.env.TEST_WORKER_INDEX;
      else process.env.TEST_WORKER_INDEX = original;
    }
  });

  test("a missing worker index is treated as the only process", async () => {
    const original = process.env.TEST_WORKER_INDEX;
    try {
      // Run outside Playwright's worker pool there is one process, so 0 is the
      // right answer rather than a fallback that happens to work.
      delete process.env.TEST_WORKER_INDEX;

      const value = uniqueDigits(9);

      expect(value).toHaveLength(9);
      expect(value).toMatch(/^\d{9}$/);
      expect(value.charAt(0)).toBe("0");
    } finally {
      if (original === undefined) delete process.env.TEST_WORKER_INDEX;
      else process.env.TEST_WORKER_INDEX = original;
    }
  });

  test("a worker mints 1000 distinct SSNs with the clock frozen", async () => {
    // Review finding B1, pinned. The first version of the worker fix took the
    // room for the worker digit out of the counter, leaving two digits -- so the
    // 101st call inside one timestamp bucket reused an SSN. Forty calls against a
    // live clock did not catch it, because the timestamp kept changing and hid
    // the wrap.
    //
    // The clock is frozen here so the counter is the ONLY thing that can vary.
    // That is the condition the defect needed, and it is the condition a real
    // burst of fixture creation inside one millisecond approximates.
    const realNow = Date.now;
    try {
      Date.now = () => 1_800_000_000_000;

      const seen = new Set(
        Array.from({ length: 1000 }, () => fictionalApplicant("burst", true, 90_000).ssn),
      );

      expect(seen.size).toBe(1000);
    } finally {
      Date.now = realNow;
    }
  });

  test("the wrap point is where the counter says, not somewhere earlier", async () => {
    // States the boundary rather than leaving it to be discovered. The counter
    // is three digits, so the 1001st call in a frozen bucket is the first that
    // can repeat -- and asserting that is what makes the 1000 above a measured
    // margin instead of a hopeful one.
    const realNow = Date.now;
    try {
      Date.now = () => 1_900_000_000_000;

      const first = fictionalApplicant("wrap", true, 90_000).ssn;
      const following = new Set(
        Array.from({ length: 999 }, () => fictionalApplicant("wrap", true, 90_000).ssn),
      );

      // Nothing in the next 999 repeats the first.
      expect(following.has(first)).toBe(false);

      // The one after that does, because the counter has come round. If this
      // ever stops being true the margin has changed and this test should be
      // updated deliberately.
      expect(fictionalApplicant("wrap", true, 90_000).ssn).toBe(first);
    } finally {
      Date.now = realNow;
    }
  });

  test("the SSN keeps its decision-band last digit", async () => {
    // Guard the guard: the namespace change must not disturb what the digit
    // means. decision-service's stub scores an even final digit into the
    // approve band and an odd one into deny/refer, and several specs choose a
    // path with it.
    expect(fictionalApplicant("even", true, 90_000).ssn.endsWith("0")).toBe(true);
    expect(fictionalApplicant("odd", false, 90_000).ssn.endsWith("1")).toBe(true);
  });
});

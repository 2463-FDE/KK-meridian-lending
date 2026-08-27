# Fairness-data policy (client) — demonstration only

**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Scope:** Meridian Lending training demonstration. Not a production collection program. Not legal advice.

## Rules

1. **Do not collect real protected-class data** for this demonstration.
2. **Do not create or use a proxy** for a protected class. ZIP, ZIP3, name, neighborhood, and similar fields are not validated proxies here and must not be treated as such.
3. **Explicit protected-class labels may exist only** in `fixtures/synthetic-offline-fairness-evaluation.csv`. That file is wholly synthetic, de-identified, audit-only, and excluded from:
   - model inputs;
   - runtime application decisions;
   - production-like records;
   - learner or client operational data;
   - traces and operational telemetry;
   - final consumer output.
4. **Do not manufacture, infer, or synthesize** protected-class attributes to “close a gap” in scoring or notices.
5. **No production or real-world fairness claim** may be made from this packet, the ZIP3 outcome screen, or the 32-row fixture.
6. **Retention and access:** the fairness fixture is retained only with this training packet. Access is staff review for audit of isolation. Borrowers must not receive it. It is discarded with the packet when a real vendor packet replaces this one, unless a later client record says otherwise.
7. Outcome monitoring that uses geography is **not** model fairness evidence and is **not** a protected-class analysis.

## Human review

Any request to add real protected-class collection, to build a proxy, or to move fixture labels into scoring or traces is a **stop and escalate**. Reviewers do not approve a workaround.

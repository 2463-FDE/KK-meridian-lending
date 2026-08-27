# Synthetic vendor-governance client-input package (training only)

**Packet name:** Kalabe-Synthetic-Vendor-Governance-Client-Inputs-Only-2026-08-24  
**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Status:** Synthetic training fixture. Not vendor-issued. Not approved for real use.

## What this is

This folder is a **wholly synthetic, training-only** client-input packet for Meridian Lending (Kalabe) Week 8 vendor-governance practice. It stands in for vendor-issued reason-code taxonomy, approved consumer wording, a model card, validation and fairness summaries, client policies, acceptance evaluations, and isolated negative fixtures.

It is **not**:

- legal advice;
- an implementation design;
- a real vendor document, contract, or approval;
- production or real-world fairness validation;
- a consumer notice, live configuration, or shipped Meridian artifact.

## Replacement before real use

No real vendor or approved vendor packet has been identified for Meridian's licensed scorer. **Actual vendor-issued, currently approved materials must replace this packet before any real use.** Until that replacement, treat every file here as a disposable training fixture.

If a real vendor document and this packet disagree, stop. The real vendor-issued current document wins over this synthetic packet. Unresolved conflicts escalate; they are not resolved by paraphrasing, nearest-match mapping, or a generic reason.

## How to use this packet in the demonstration

Use it only as **client inputs and acceptance outcomes**:

1. Reason codes and approved wording are the only consumer-facing reason source for this training simulation.
2. An unmapped, missing, generic, opaque, invented, or post-hoc reason is a refusal, not a notice.
3. Explicit protected-class labels exist only in `fixtures/synthetic-offline-fairness-evaluation.csv`. That file is audit-only and must not enter model or runtime inputs, application decisions, traces, or consumer output.
4. Negative fixtures under `evaluations/fixtures/` are never approved inputs.
5. Do not generate a consumer adverse-action notice unless a separately defined demonstration scope explicitly calls for one.

## Roles this packet assumes (shipped Meridian terms)

Shipped session roles on current Meridian `main` are **borrower**, **csr**, **underwriter**, and **admin**. Staff roles are csr, underwriter, and admin. Money-moving servicing actions are csr/admin only. This packet does not change those roles.

## Regulatory posture (not legal advice)

Live adverse-action notification content is grounded in current **12 CFR 1002.9** (Regulation B). CFPB Circulars **2022-03** and **2023-03** were **withdrawn** on 12 May 2025 and are not current authority. See `sources/regulatory-notes.md` and `sources/source-ledger.csv`.

## Folder map

| Path | Purpose |
|---|---|
| `vendor/` | Synthetic vendor profile, reason-code taxonomy, approved wording, model card, validation and fairness summaries |
| `policies/` | Client fairness-data, document-precedence, and adverse-action boundaries |
| `fixtures/` | Isolated offline fairness-evaluation rows only |
| `evaluations/` | Client acceptance cases and isolated negative fixtures |
| `sources/` | Citation ledger and regulatory-status notes |
| `PACKAGE-INVENTORY.txt` | File list and sizes |
| `SHA256SUMS.txt` | Checksums of every other file in this folder |

## Versioning

Within a document tier, the current version and effective date win. This packet's version is **CCUS-SYN-2026.08.24**, effective **2026-08-24**. A later real vendor version supersedes it entirely.

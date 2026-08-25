"""The AI summary reports what the applicant ASKED FOR, not what was financed.

This currently holds, and this file exists so it keeps holding. The amount
financed is now visible on both the borrower's disclosure and the underwriting
screen, broken down into the requested principal less the prepaid fee -- which
makes "use the amount financed here too, for consistency" an obvious-looking and
wrong change to make.

Wrong for two reasons.

**A summary describes an APPLICATION, and an application has no amount
financed.** The summary is produced for staff reading a file that may not have
an offer at all -- referred, declined, or still in intake. `amount_financed`
exists only once an offer is generated, so a summary sourced from it would be
empty for exactly the applications a human is most likely to be reading.

**The two numbers answer different questions.** "$9,000 requested over 24
months" is the application. "$8,730 financed" is the contract, net of a prepaid
fee the borrower never receives. An underwriter reading "the applicant requested
$8,730" is reading a figure the applicant never wrote down, and would compare it
against an income and a purpose stated for a different amount.
"""
import pathlib

import pytest

from app import llm_client


REPO = pathlib.Path(__file__).resolve().parents[3]


def test_the_model_is_asked_for_the_requested_amount():
    """The field the model returns is named and described as the requested
    amount, not the financed one."""
    field = llm_client._LLMOutput.model_fields["loan_amount"]

    assert "equest" in (field.description or ""), (
        f"loan_amount is described as {field.description!r}; the summary reports "
        f"what the applicant applied for, and a description that does not say so "
        f"invites sourcing it from the offer instead")


def test_no_offer_derived_amount_reaches_the_summary_prompt():
    """The allowed application fields, asserted as a set rather than a scan.

    `amount` is the applicant's own figure. `amount_financed`, `origination_fee`
    and `requested_principal` are offer-derived: the first two do not exist until
    an offer is generated, and the third is a restatement of `amount` through a
    different service. Adding any of them here would put a contract figure into a
    summary of an application.
    """
    allowed = set(llm_client._PROMPT_ALLOWED_FIELDS)

    for offer_field in ("amount_financed", "origination_fee",
                        "requested_principal", "total_of_payments",
                        "finance_charge", "apr"):
        assert offer_field not in allowed, (
            f"{offer_field!r} is passed to the summary prompt. It is a term of an "
            f"OFFER, and the summary describes an APPLICATION -- which may not "
            f"have one")

    assert "amount" in allowed, (
        "the applicant's own requested amount is no longer passed to the summary")


def test_the_assistant_service_never_reads_an_offer_amount():
    """A whole-service check, because the field could arrive by a route this
    file has not thought of -- a new upstream call, a widened projection."""
    offenders = []
    for path in sorted((REPO / "services" / "loan-assistant" / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            for offer_field in ("amount_financed", "origination_fee",
                                "requested_principal"):
                if offer_field in line:
                    offenders.append(
                        "%s:%d %s" % (path.relative_to(REPO).as_posix(), lineno,
                                      stripped[:100]))

    assert not offenders, (
        "loan-assistant reads an offer-derived amount:\n" + "\n".join(offenders)
        + "\n\nThe summary describes an application. A borrower who applied for "
          "$9,000 must not be summarised as having requested $8,730, which is "
          "the amount financed after a prepaid fee they never receive")

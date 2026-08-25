"""The LOS forwards the breakdown, and forwards its absence as absence.

`_to_offer_out` is the mapping layer between disclosure-service's response and
the shape the browser reads, and it has dropped a field before: `note_rate_pct`
arrived and was not forwarded, which is what let a 5.43% "APR" sit under a 7.99%
loan without looking wrong. A new pair of fields going through the same function
is the same risk, so it is asserted rather than assumed.

The distinction that matters is null-versus-zero. Four of the amounts use
`.get(x, 0)` legitimately -- an offer that reaches this point has them, and the
integrity check refuses the row otherwise. These two must NOT: a legacy offer
reports no breakdown, and a defaulted `0` would render as "amount you asked for
$0.00, less origination fee $0.00", which is a disclosure nobody can support.
"""
from app.routers.offers import _to_offer_out


def _response(**disclosure_overrides):
    """A disclosure-service OfferResponse, as this mapper receives it."""
    disclosure = {
        "note_rate_pct": 7.99, "apr": 9.584, "finance_charge": 1174.46,
        "monthly_payment": 407.12, "amount_financed": 8730.00,
        "total_of_payments": 9770.88, "regular_payment_count": 23,
        "final_payment": 407.10, "term_months": 24,
        "requested_principal": 9000.00, "origination_fee": 270.00,
    }
    disclosure.update(disclosure_overrides)
    return {"disclosure": disclosure, "schedule": [],
            "schedule_source": "contract", "schedule_note": None}


def test_the_breakdown_is_forwarded():
    out = _to_offer_out(10, _response())

    assert out.disclosure.requested_principal == 9000.00
    assert out.disclosure.origination_fee == 270.00


def test_the_forwarded_breakdown_still_foots():
    """Nothing is recalculated in transit. Asserted because a mapper is exactly
    where someone would helpfully "fix" a rounding difference."""
    out = _to_offer_out(10, _response())
    d = out.disclosure

    assert round(d.requested_principal - d.origination_fee, 2) == round(
        d.amount_financed, 2)


def test_a_missing_breakdown_is_forwarded_as_null_not_zero():
    """The legacy offer. `.get(x, 0)` here would turn "we cannot show you this"
    into "your fee was $0.00"."""
    out = _to_offer_out(10, _response(requested_principal=None,
                                      origination_fee=None))

    assert out.disclosure.requested_principal is None
    assert out.disclosure.origination_fee is None


def test_an_upstream_that_sends_neither_field_still_maps():
    """An older disclosure-service, or a cached response predating the field.
    The mapper must not raise, and must not invent a breakdown."""
    payload = _response()
    del payload["disclosure"]["requested_principal"]
    del payload["disclosure"]["origination_fee"]

    out = _to_offer_out(10, payload)

    assert out.disclosure.requested_principal is None
    assert out.disclosure.origination_fee is None


def test_a_zero_fee_breakdown_survives_the_mapping():
    """`0.0` is falsy, and this is the layer most likely to lose it to a
    truthiness check."""
    out = _to_offer_out(10, _response(requested_principal=5000.00,
                                      origination_fee=0.00,
                                      amount_financed=5000.00))

    assert out.disclosure.origination_fee == 0.00
    assert out.disclosure.origination_fee is not None


def test_the_mapper_does_no_arithmetic_of_its_own():
    """It forwards. A second implementation of the fee calculation here is how
    the fee percentage reached three different values in three files (D6), and
    this mapper is on the path between the server that owns the figure and the
    browser that displays it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_to_offer_out).lstrip())
    fn = tree.body[0]
    arithmetic = [node for node in ast.walk(fn)
                  if isinstance(node, ast.BinOp)
                  and isinstance(node.op, (ast.Sub, ast.Mult, ast.Div))]

    assert not arithmetic, (
        "_to_offer_out performs arithmetic. Every monetary figure it handles is "
        "computed by whoever owns it; a subtraction here is a second version of "
        "financial truth on the path to the borrower's screen")

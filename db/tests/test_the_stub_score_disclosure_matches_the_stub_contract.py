"""The underwriting screen's stub disclosure must key on the real stub marker.

`/underwriting/{appId}` tells a reader that the underwriting model score is
derived from the credit bureau score and stated income -- but ONLY for the
deterministic demo stub, because only the stub is computed that way
(`_stub_model_score`). A licensed scorer's number is the provider model's own
output and the copy must not claim that formula for it.

The screen tells the two apart by the `-stub` suffix on `model_version`, which
is the contract RF-1 established in `decision-service`. This ties the two
together so the disclosure cannot quietly stop matching:

* if `decision.py` stops suffixing stub versions, the screen would show a
  licensed-scorer surface for a stub run
* if the frontend stops checking the suffix, it would claim the stub's formula
  for whatever scored the application

Both directions are a false statement about how a number was produced, on a
screen whose entire purpose is evidence.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
DECISION = REPO / "services" / "decision-service" / "app" / "decision.py"
SCREEN = REPO / "frontend" / "app" / "underwriting" / "[appId]" / "page.tsx"

#: The marker itself is derived below; this is only the shape it must have.
STUB_VERSION_PATTERN = re.compile(r'"model_version":\s*f"\{AI_MODEL_VERSION\}(-[a-z]+)"')


def _stub_suffix() -> str:
    """The suffix `decision-service` appends to a stub `model_version`."""
    matches = STUB_VERSION_PATTERN.findall(DECISION.read_text(encoding="utf-8"))
    assert matches, (
        "decision.py no longer builds a stub model_version as "
        'f"{AI_MODEL_VERSION}-<suffix>". The underwriting screen identifies a '
        "stub run by that suffix, so this is not a cosmetic change."
    )
    assert len(set(matches)) == 1, (
        f"decision.py uses more than one stub suffix ({sorted(set(matches))}); "
        "the screen checks for one."
    )
    return matches[0]


def test_the_screen_checks_the_suffix_the_scorer_actually_appends():
    suffix = _stub_suffix()
    screen = SCREEN.read_text(encoding="utf-8")
    assert f'endsWith("{suffix}")' in screen, (
        f"the underwriting screen does not test model_version for {suffix!r}, "
        "which is what decision.py appends to a stub version. Without that "
        "check the stub's derivation is claimed for whatever scored the "
        "application."
    )


def test_the_stub_really_is_derived_from_the_bureau_score_and_income():
    """The disclosure states a formula, so the formula must still be that.

    `_stub_model_score(bureau_score, income)` is what makes "derived from the
    credit bureau score and stated income" a true sentence. If the stub is ever
    rewritten to read anything else, the sentence becomes a false claim about
    how a displayed number was produced.
    """
    source = DECISION.read_text(encoding="utf-8")
    signature = re.search(
        r"def _stub_model_score\(([^)]*)\)", source)
    assert signature, "_stub_model_score is gone; the screen still describes it"
    params = [p.split(":")[0].strip() for p in signature.group(1).split(",")]
    assert params == ["bureau_score", "income"], (
        "_stub_model_score no longer takes exactly (bureau_score, income), so "
        "the underwriting screen's sentence 'derived from the credit bureau "
        f"score and stated income' may no longer be true. It now takes: {params}"
    )

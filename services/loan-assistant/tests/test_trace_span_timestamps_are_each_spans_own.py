"""Every emitted child carries ITS OWN start and end, and never ends first.

TRC-02, observed in LangSmith on a real summary request. Every child of one
`underwriting_summary` run reported the same duration, and the duration was
NEGATIVE:

    underwriting_summary
      request           -14.73s
      model             -14.73s
      policy_retrieval  -14.73s
      outcome           -14.73s
      agent_run         -14.73s
      validation        -14.73s

Two defects in one line of `emit`. Every child was created with
`start_time=_dt(started)` -- the ROOT's start -- so per-stage timing was
unreadable even when it was positive. And because the SDK stamps a child's start
at creation time, the explicit end computed from the ROOT's clock
(`started + duration_ms`) landed BEFORE it. The uniform -14.73s was the
request's own elapsed time, on a request that took about 13.9 seconds.

A duration cannot be negative, so a trace showing one is not a slow trace, it is
a wrong one. This module exists to make what is emitted trustworthy, so a
regression here is not cosmetic.

**WHY THE TIMESTAMPS ARE DISTINCT AND NON-ZERO IN THESE CASES.** A test built
from `record()`-only stages would pass with the bug still present: those spans
have `ended_at == started_at`, so root-start-for-everything still produces
0, not a negative. The spans below are given deliberately different, deliberately
non-zero windows, and the cases assert that each child's pair matches ITS OWN
span rather than the root's -- which is the property that was broken.

Privacy is unchanged and re-asserted here rather than assumed: `inputs` stays
empty, outputs stay the filtered categorical payload, and the two new payload
keys are timestamps, which describe when the system did something.
"""
import uuid

import pytest

from app import trace as trace_mod


class _RecordedRun:
    """Stands in for `langsmith.run_trees.RunTree`, recording what it is told.

    Deliberately NOT a mock that accepts anything: it records the arguments the
    emitter actually passes, so a case can assert on them. It also mimics the
    one SDK behaviour that caused TRC-02 -- a child's start is stamped when the
    child is created -- by recording creation order, so a future change that
    goes back to relying on that is visible here.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.children = []
        self.ended_with = None
        self.posted = False
        # The SDK behaviour that CAUSED TRC-02, emulated on purpose: a run whose
        # `start_time` is not supplied is stamped when it is created. Without
        # this, a change that stops passing `start_time` would leave the
        # ordering case below unable to fail -- it would compare a missing start
        # against a supplied end and pass by not looking. With it, dropping
        # `start_time` reproduces the real negative duration.
        if kwargs.get("start_time") is None:
            import datetime
            self.kwargs["start_time"] = datetime.datetime.now(datetime.timezone.utc)

    def create_child(self, **kwargs):
        child = _RecordedRun(**kwargs)
        self.children.append(child)
        return child

    def post(self):
        self.posted = True

    def patch(self):
        pass

    def end(self, **kwargs):
        self.ended_with = kwargs

    @property
    def name(self):
        return self.kwargs.get("name")

    @property
    def start_time(self):
        return self.kwargs.get("start_time")

    @start_time.setter
    def start_time(self, value):
        # The real SDK allows assignment, and the emitter now uses it: passing
        # `start_time` to `create_child` is dropped when the parent came from
        # headers, so the value has to be assigned as well. Recording the
        # assignment keeps this stand-in faithful to the object it replaces.
        self.kwargs["start_time"] = value

    @property
    def end_time(self):
        return (self.ended_with or {}).get("end_time")


@pytest.fixture()
def emitted(monkeypatch):
    """Emit one trace with controlled span windows and return the root run."""
    monkeypatch.setattr(trace_mod, "is_enabled", lambda: True)

    captured = {}

    class _Factory:
        def __call__(self, **kwargs):
            root = _RecordedRun(**kwargs)
            captured["root"] = root
            return root

    # `emit` imports RunTree inside the function, so the module it imports from
    # is what has to be patched.
    import langsmith.run_trees as rt
    monkeypatch.setattr(rt, "RunTree", _Factory())

    t = trace_mod.SummaryTrace()
    # A fixed, obviously-synthetic base so the arithmetic is checkable by eye.
    base = 1_700_000_000.0
    t.started_at = base

    #: stage -> (start offset, end offset) in seconds. Deliberately different
    #: from each other, none zero-length, and none starting at the root's start
    #: except the first -- so "everything got the root's clock" cannot pass.
    windows = {
        "request": (0.00, 0.05),
        "agent_run": (0.10, 5.40),
        "policy_retrieval": (5.45, 6.10),
        "model": (6.20, 12.80),
        "validation": (12.85, 12.99),
        "outcome": (13.00, 13.02),
    }
    for stage, (s, e) in windows.items():
        span = trace_mod._Span(stage, {"stage": stage})
        span.id = str(uuid.uuid4())
        span.started_at = base + s
        span.ended_at = base + e
        t.spans.append(span)

    trace_mod.emit(t)
    root = captured["root"]
    return root, t, base, windows


def test_the_emitter_produced_a_child_for_every_span(emitted):
    """Guard the guard. Every assertion below iterates the children."""
    root, t, _base, windows = emitted
    assert root.posted, "the root run was never posted"
    assert len(root.children) == len(windows), (
        "expected one child per span, got %d for %d spans -- the cases below "
        "would otherwise pass over a short list"
        % (len(root.children), len(windows)))


def test_no_emitted_child_ends_before_it_starts(emitted):
    """The invariant TRC-02 broke. This is the case that must never regress."""
    root, _t, _base, _w = emitted
    for child in root.children:
        assert child.start_time is not None, "%s has no start_time" % child.name
        assert child.end_time is not None, "%s has no end_time" % child.name
        assert child.start_time <= child.end_time, (
            "%s ends before it starts (%s > %s) -- LangSmith renders that as a "
            "negative duration, which is what TRC-02 was"
            % (child.name, child.start_time, child.end_time))


def test_each_child_carries_its_own_window_not_the_roots(emitted):
    """The other half of TRC-02: the timing has to be per stage.

    Asserted against the exact synthetic windows, so a child silently given the
    root's clock fails here even when the ordering happens to stay valid.
    """
    root, _t, base, windows = emitted
    by_name = {c.name: c for c in root.children}
    for stage, (s, e) in windows.items():
        child = by_name[stage]
        assert child.start_time.timestamp() == pytest.approx(base + s, abs=0.002), (
            "%s started at the wrong instant -- expected its own start" % stage)
        assert child.end_time.timestamp() == pytest.approx(base + e, abs=0.002), (
            "%s ended at the wrong instant" % stage)


def test_the_children_do_not_all_share_one_start(emitted):
    """The visible symptom, stated as its own case.

    Every child having the same start is what made per-stage timing unreadable,
    and it is possible to have valid orderings and still be useless. With real
    windows the starts must differ.
    """
    root, _t, _base, _w = emitted
    starts = {c.start_time for c in root.children}
    assert len(starts) == len(root.children), (
        "%d children share only %d distinct start times"
        % (len(root.children), len(starts)))


def test_durations_are_positive_and_distinct(emitted):
    """A stage that took 6.6s and one that took 0.02s must not look alike."""
    root, _t, _base, _w = emitted
    durations = [(c.end_time - c.start_time).total_seconds() for c in root.children]
    assert all(d >= 0 for d in durations), "negative duration: %s" % durations
    assert max(durations) > 1.0, (
        "no stage shows a substantial duration, so this suite would not notice "
        "timing being flattened: %s" % durations)
    assert len(set(round(d, 3) for d in durations)) > 1, (
        "every stage reports the same duration: %s" % durations)


def test_payload_carries_each_spans_timestamps(emitted):
    """The transport half: `payload()` has to expose what `emit` needs."""
    _root, t, base, windows = emitted
    spans = {s["name"]: s for s in t.payload()["spans"]}
    for stage, (s, e) in windows.items():
        assert spans[stage]["started_at"] == pytest.approx(base + s, abs=0.002)
        assert spans[stage]["ended_at"] == pytest.approx(base + e, abs=0.002)
        assert spans[stage]["ended_at"] >= spans[stage]["started_at"]


def test_a_stage_with_no_end_falls_back_to_its_own_start(monkeypatch):
    """`record()` stages have no separate end; they must not become negative.

    `record` sets `ended_at = started_at`, but `payload` must not depend on that
    remaining true -- a `None` end has to fall back to the span's own start, not
    to the root's.
    """
    t = trace_mod.SummaryTrace()
    t.started_at = 1_700_000_000.0
    span = trace_mod._Span("request", {"stage": "request"})
    span.started_at = t.started_at + 9.0
    span.ended_at = None
    t.spans.append(span)

    emitted_span = t.payload()["spans"][0]

    assert emitted_span["started_at"] == pytest.approx(t.started_at + 9.0, abs=0.002)
    assert emitted_span["ended_at"] == emitted_span["started_at"], (
        "a stage with no recorded end must fall back to its OWN start; falling "
        "back to the root's would place it before itself")
    assert emitted_span["duration_ms"] == 0


def test_privacy_is_unchanged_by_the_timing_fix(emitted):
    """Timestamps are the only thing added. Nothing else may have travelled."""
    root, _t, _base, _w = emitted
    assert root.kwargs.get("inputs") == {}, "the root sent non-empty inputs"
    for child in root.children:
        assert child.kwargs.get("inputs") == {}, (
            "%s sent non-empty inputs" % child.name)
        outputs = child.kwargs.get("outputs") or {}
        for key in outputs:
            assert key in trace_mod.ALLOWED_FIELDS, (
                "%s emitted a field outside the allow-list: %s"
                % (child.name, key))


# ---------------------------------------------------------------------------
# The same invariant, against the REAL SDK and a REAL distributed parent.
#
# The cases above use a recorder, and a recorder cannot catch what actually
# shipped TRC-02 twice: `RunTree.create_child` SILENTLY IGNORES `start_time`
# when the parent was rebuilt by `RunTree.from_headers`. The child inherits the
# parent's start instead. Verified against langsmith 0.10.5 -- a
# locally-constructed parent honours the argument, a from_headers parent does
# not.
#
# The gateway path is ALWAYS the from_headers one, so the first fix passed every
# recorder-based case and every non-parented live check, and still emitted
# negative durations in production. These cases exercise the real objects so the
# assertion sits at the boundary where the defect lives.
# ---------------------------------------------------------------------------

def _real_tree_emission(monkeypatch, base):
    """Emit through the real `RunTree`, with a from_headers parent, no network."""
    from langsmith.run_trees import RunTree

    posted = []
    monkeypatch.setattr(RunTree, "post", lambda self, *a, **k: posted.append(self))
    monkeypatch.setattr(RunTree, "patch", lambda self, *a, **k: None)
    monkeypatch.setattr(trace_mod, "is_enabled", lambda: True)

    import datetime
    gw = RunTree(name="gateway_entry", run_type="chain", inputs={},
                 start_time=datetime.datetime.now(datetime.timezone.utc))
    headers = dict(gw.to_headers())

    t = trace_mod.SummaryTrace(parent_headers=headers)
    t.started_at = base
    for stage, (s, e) in (("agent_run", (0.10, 5.40)),
                          ("policy_retrieval", (5.45, 6.10)),
                          ("model", (6.20, 12.80)),
                          ("outcome", (12.85, 12.85))):
        span = trace_mod._Span(stage, {"stage": stage})
        span.id = str(uuid.uuid4())
        span.started_at = base + s
        span.ended_at = base + e
        t.spans.append(span)

    trace_mod.emit(t)
    return [r for r in posted if r.name != "gateway_entry"]


def test_no_real_runtree_child_ends_before_it_starts_under_a_header_parent(monkeypatch):
    """The production shape. This is the case the recorder could not provide."""
    base = 1_700_000_000.0
    runs = _real_tree_emission(monkeypatch, base)

    assert runs, "nothing was posted, so this case would pass by checking nothing"
    for run in runs:
        assert run.start_time is not None, "%s has no start_time" % run.name
        end = run.end_time
        if end is None:
            continue
        assert run.start_time <= end, (
            "%s ends before it starts (%s > %s) under a from_headers parent -- "
            "this is TRC-02, and it is what `create_child` dropping "
            "`start_time` produces" % (run.name, run.start_time, end))


def test_the_real_root_keeps_its_own_start_under_a_header_parent(monkeypatch):
    """`underwriting_summary` reported ~0.001s in production for this reason.

    Inheriting the gateway's start made the root's own span invisible, which is
    the half of TRC-02 that survived the first fix.
    """
    base = 1_700_000_000.0
    runs = _real_tree_emission(monkeypatch, base)
    root = [r for r in runs if r.name == "underwriting_summary"]

    assert root, "the summary root was never posted"
    assert root[0].start_time.timestamp() == pytest.approx(base, abs=0.002), (
        "the root inherited its parent's start instead of keeping its own")


def test_each_real_child_keeps_its_own_window_under_a_header_parent(monkeypatch):
    """Per-stage timing has to survive the distributed parent too."""
    base = 1_700_000_000.0
    runs = _real_tree_emission(monkeypatch, base)
    by_name = {r.name: r for r in runs}

    for stage, (s, _e) in (("agent_run", (0.10, 5.40)),
                           ("policy_retrieval", (5.45, 6.10)),
                           ("model", (6.20, 12.80))):
        assert stage in by_name, "%s was not posted" % stage
        assert by_name[stage].start_time.timestamp() == pytest.approx(
            base + s, abs=0.002), (
            "%s did not keep its own start under a from_headers parent" % stage)


# ---------------------------------------------------------------------------
# TRC-NEG-ROOT: the ROOT's outputs must not carry a negative duration either.
#
# `emit` used to clamp an inverted window when building the CHILD run, while
# `root.end(outputs=payload, ...)` still shipped the original `ended_at` and a
# negative `duration_ms`. So a negative duration could still reach LangSmith --
# in the root's outputs rather than in a child's times -- and the two disagreed
# about the same span.
#
# Normalisation now happens once, in `payload()`, so the children and the root
# read the same values and cannot diverge. These cases pin that.
# ---------------------------------------------------------------------------

def _trace_with_an_inverted_span(started=100.0, ended=99.5):
    t = trace_mod.SummaryTrace()
    t.started_at = started
    span = trace_mod._Span("request", {"stage": "request"})
    span.id = str(uuid.uuid4())
    span.started_at = started
    span.ended_at = ended
    t.spans.append(span)
    return t


def test_payload_never_carries_an_inverted_window():
    """The exact reproduction from review: 100.0 -> 99.5 gave duration_ms -500."""
    spans = _trace_with_an_inverted_span().payload()["spans"]

    assert spans, "no span in the payload, so this case checked nothing"
    for s in spans:
        assert s["ended_at"] >= s["started_at"], (
            "%s: payload carries ended_at %s before started_at %s"
            % (s["name"], s["ended_at"], s["started_at"]))
        assert s["duration_ms"] >= 0, (
            "%s: payload carries a negative duration_ms (%d) -- the root ships "
            "this dict as its outputs" % (s["name"], s["duration_ms"]))


def test_no_span_in_a_normal_payload_is_negative_either(emitted):
    """The same assertion on the ordinary path, so it is not only about
    inverted input."""
    _root, t, _base, _w = emitted
    for s in t.payload()["spans"]:
        assert s["ended_at"] >= s["started_at"]
        assert s["duration_ms"] >= 0


def test_the_root_outputs_agree_with_the_child_run_times(monkeypatch):
    """The disagreement TRC-NEG-ROOT described, asserted directly.

    Whatever the root ships as `outputs` has to describe the same windows the
    children were given -- otherwise one of the two is lying about the same
    span.
    """
    monkeypatch.setattr(trace_mod, "is_enabled", lambda: True)
    captured = {}

    class _Factory:
        def __call__(self, **kwargs):
            root = _RecordedRun(**kwargs)
            captured["root"] = root
            return root

    import langsmith.run_trees as rt
    monkeypatch.setattr(rt, "RunTree", _Factory())

    t = _trace_with_an_inverted_span()
    trace_mod.emit(t)
    root = captured["root"]

    shipped = {s["name"]: s for s in (root.ended_with or {}).get("outputs", {}).get("spans", [])}
    assert shipped, "the root shipped no spans in its outputs"
    for child in root.children:
        s = shipped[child.name]
        assert s["started_at"] == pytest.approx(child.start_time.timestamp(), abs=0.002), (
            "%s: the root's outputs and the child's run time disagree about the "
            "start" % child.name)
        assert s["ended_at"] == pytest.approx(child.end_time.timestamp(), abs=0.002), (
            "%s: the root's outputs and the child's run time disagree about the "
            "end" % child.name)
        assert s["duration_ms"] >= 0

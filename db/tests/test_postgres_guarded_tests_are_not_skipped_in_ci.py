"""A proof that skips is not a proof. If CI runs it, CI must supply the database.

Every real-Postgres test in this repository is guarded the same way:

    pytestmark = pytest.mark.skipif(not DATABASE_URL, reason=...)

That guard is right for a developer without a database. It is also silent: a job
that runs those files with no `DATABASE_URL` reports green having asserted
nothing, and the file that would have caught the regression looks exactly as
passing as one that ran.

This is not hypothetical here. `ci.yml`'s own backend job carries the scar:

    "the real-Postgres tests ... are all guarded by `skipif(not DATABASE_URL)`.
     No DATABASE_URL was set here, so every one of them SKIPPED in CI and had
     only ever run on a developer machine."

The variable was added to that job afterwards. Nothing stops it being dropped
again, or a new job being added that runs guarded files without it -- which is
how the same defect returns through an omission rather than an edit.

So the expectation is DERIVED, not listed. The guarded files come from the
filesystem, the jobs and what they run come from `ci.yml`, and this test names
neither a service nor a variable location of its own. Adding a guarded test to a
covered directory needs no edit here; adding a job that runs guarded tests
without a database fails here.

What this does NOT do is require Postgres locally. The guard stays exactly as it
is, a developer with no database still skips, and this test reads files rather
than connecting to anything -- it is itself unguarded for that reason.
"""
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"

#: The variable whose absence turns a proof into a skip. Read out of the guard
#: expression below rather than assumed, so a rename cannot leave this stale.
GUARD_RE = re.compile(r"skipif\(\s*not\s+(\w+)\b")

#: ...but only when that variable came from the ENVIRONMENT. Several suites skip
#: on something the checkout itself supplies -- a deck, an ADR, a committed
#: payload directory -- and those are not this file's business: no CI variable
#: would make them run, and demanding one would be inventing an expectation.
#: What this file is about is a proof that disappears because the JOB was
#: configured without a database.
ENV_READ_RE = re.compile(
    r"^(\w+)\s*=\s*os\.(?:environ\.get|getenv)\(", re.MULTILINE
)

#: A pytest invocation, with whatever paths it was given. `-` options are
#: dropped: what matters is which directories the run covers.
PYTEST_RE = re.compile(r"(?:python\s+-m\s+)?pytest\b([^\n|;&]*)")

CD_RE = re.compile(r"^\s*cd\s+(\S+)", re.MULTILINE)
MATRIX_REF_RE = re.compile(r"\$\{\{\s*matrix\.(\w+)\s*\}\}")


def _guarded_test_files() -> dict[pathlib.Path, str]:
    """Every test file that skips itself when an environment variable is unset.

    Returns path -> variable name. Derived by reading the guards themselves, so
    a file that stops being guarded stops being expected here. A guard counts
    only when the name it tests was read out of `os.environ` in the same file --
    see `ENV_READ_RE`.
    """
    found: dict[pathlib.Path, str] = {}
    for py in REPO.rglob("test_*.py"):
        parts = py.parts
        if "__pycache__" in parts or "node_modules" in parts or ".venv" in parts:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        from_env = set(ENV_READ_RE.findall(src))
        for name in GUARD_RE.findall(src):
            if name in from_env:
                found[py.relative_to(REPO)] = name
                break
    return found


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _matrix_expansions(job: dict) -> list[dict[str, str]]:
    """Every concrete matrix combination for a job, or one empty combination."""
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    axes = {k: v for k, v in matrix.items() if isinstance(v, list)}
    if not axes:
        return [{}]
    combos = [{}]
    for key, values in axes.items():
        combos = [{**c, key: str(v)} for c in combos for v in values]
    return combos


def _covered_dirs(job: dict) -> set[pathlib.PurePosixPath]:
    """Repo-relative directories this job's pytest invocations actually run.

    Read out of the job's own `run` scripts: the working directory each `cd`
    establishes, and the paths each `pytest` is handed (its cwd when handed
    none).
    """
    covered: set[pathlib.PurePosixPath] = set()
    for step in job.get("steps") or []:
        script = step.get("run")
        if not script:
            continue
        for combo in _matrix_expansions(job):
            expanded = MATRIX_REF_RE.sub(
                lambda m: combo.get(m.group(1), m.group(0)), script
            )
            cwd = pathlib.PurePosixPath(".")
            for line in expanded.splitlines():
                cd = CD_RE.match(line)
                if cd:
                    cwd = cwd / cd.group(1)
                    continue
                run = PYTEST_RE.search(line)
                if not run:
                    continue
                targets = [
                    a for a in run.group(1).split() if not a.startswith("-")
                ]
                if targets:
                    covered |= {cwd / t for t in targets}
                else:
                    covered.add(cwd)
    return {pathlib.PurePosixPath(str(c).removeprefix("./")) for c in covered}


def _env_of(job: dict) -> set[str]:
    """Variables the job sets for every step, plus any a step sets for itself."""
    names = set((job.get("env") or {}).keys())
    for step in job.get("steps") or []:
        names |= set((step.get("env") or {}).keys())
    return names


def _jobs_running(test_file: pathlib.Path) -> list[tuple[str, dict]]:
    posix = pathlib.PurePosixPath(test_file.as_posix())
    running = []
    for name, job in _jobs().items():
        for covered in _covered_dirs(job):
            if posix == covered or covered in posix.parents:
                running.append((name, job))
                break
    return running


def test_there_are_postgres_guarded_tests_to_protect():
    """If this fails the derivation is broken, not the repository.

    A test that silently finds nothing to check is the same defect one level up:
    it would go green for the rest of this file's life without asserting a thing.
    """
    guarded = _guarded_test_files()
    assert len(guarded) > 20, (
        f"only {len(guarded)} guarded test files found -- the guard expression "
        f"this file greps for ({GUARD_RE.pattern}) has probably changed shape, "
        "so the check below is passing on an empty set"
    )


def test_ci_jobs_that_run_guarded_tests_supply_the_database():
    """No CI job may run a Postgres-guarded test without setting its variable."""
    offences = []
    for test_file, variable in sorted(_guarded_test_files().items()):
        for job_name, job in _jobs_running(test_file):
            if variable not in _env_of(job):
                offences.append(f"  {job_name} runs {test_file.as_posix()} with no {variable}")

    assert not offences, (
        "these CI jobs run tests that skip themselves when a variable is unset, "
        "and do not set it -- they would report green having asserted nothing:\n"
        + "\n".join(sorted(set(offences)))
    )


@pytest.mark.parametrize("suite", ["db/tests", "services/servicing-service/tests"])
def test_the_guarded_suites_are_run_by_some_ci_job(suite):
    """And the jobs have to exist at all.

    The check above is vacuously true for a suite no job runs. These two are
    named because they are where this repository's real-Postgres proofs live --
    the migration/schema guards and the servicing money paths, including the
    approvals snapshot concurrency proof.
    """
    guarded = [f for f in _guarded_test_files() if f.as_posix().startswith(suite)]
    assert guarded, f"no guarded tests found under {suite}"
    for test_file in guarded:
        assert _jobs_running(test_file), (
            f"{test_file.as_posix()} guards itself on a database and no CI job "
            "runs it, so it has only ever run on a developer machine"
        )

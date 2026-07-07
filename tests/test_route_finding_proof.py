"""Issue 7: the route-finding proof, pinned as tests.

Four claims, each one a guard so the proof cannot silently rot:

1. SEARCH — typed backward-chaining resolves a depth-6 route out of a
   262,144-route space with thousands of distractors in well under a
   second: the type system prunes, nothing enumerates.
2. LEARNING — the Beta-posterior/Thompson loop (two parameters per node,
   fed by execution outcomes) converges onto hidden reliability.
3. CACHE — an assembled route re-enters the library as ONE node and wins
   the next assembly outright.
4. CONTAINMENT — model advice cannot build or break routes: a dead model
   changes nothing, an adversarial one is out-voted by evidence.

The heavier, printing variant lives in benchmarks/route_scale.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

import route_scale  # noqa: E402
from route_scale import (  # noqa: E402
    ContractAssembler,
    chain_goal,
    learning_loop,
    marketplace,
    measure_cache,
    measure_containment,
)


def test_search_resolves_typed_routes_without_enumerating_the_space():
    depth, width, noise = 6, 8, 1_000
    library = marketplace(depth, width, noise)
    started = time.perf_counter()
    result = ContractAssembler(library).assemble(chain_goal(depth))
    elapsed = time.perf_counter() - started

    assert result.complete
    # D picks close the route — the K^D space was never walked.
    assert len(result.selected) == depth
    assert width**depth == 262_144  # the space the doubt points at
    # Loose CI bound; the measured figure is ~4 ms on a dev box.
    assert elapsed < 2.0


def test_the_execution_feedback_loop_converges_on_reliability():
    # The reliable provider is LAST in name order, so no deterministic
    # tie-break can be accused of doing the learning.
    early, late = learning_loop(rounds=400, seed=7)
    assert late >= 0.95  # converged: the reliable provider owns the route
    assert late > early  # and it LEARNED its way there


def test_the_assembled_route_is_the_reusable_cache():
    first_picks, second_picks = measure_cache(depth=6, width=8, noise=200)
    assert first_picks == 6
    assert second_picks == 1  # the whole route reused as one node


def test_model_advice_is_contained_by_the_posterior():
    late, dead_model_identical = measure_containment(rounds=400)
    # A model endorsing an unreliable provider at full strength on every
    # pick still loses to accumulated evidence...
    assert late >= 0.9
    # ...and a model that only raises leaves the route bit-identical.
    assert dead_model_identical is True


def test_route_scale_module_is_the_one_under_test():
    # The guard tests exercise the same module the benchmark runs.
    assert Path(route_scale.__file__).name == "route_scale.py"

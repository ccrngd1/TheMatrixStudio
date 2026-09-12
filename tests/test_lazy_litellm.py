# SPDX-License-Identifier: Apache-2.0
"""
The deferred ``litellm`` import, and the two properties that have to hold together.
It has to STAY deferred (or the API Lambda's cold start regresses), and the module
attribute has to remain patchable (or 132 existing tests stop testing anything).

Why this file exists at all: on 2026-09-12 a login on the deployed stack took 25
seconds to show the run list. The API worked — ``GET /api/runs`` returned 200 — but
importing the app cost 1.91 s, of which litellm was 1.70 s, and that pushed
Lambda's init phase past its hard 10 s limit. Lambda aborts and retries an init
that overruns, so two 10 s timeouts were burned before any work happened.

The regression this guards is a plausible one-line edit: someone adds ``import
litellm`` at the top of any module the API imports, and the cold start silently
goes back to 5.7 s with no test failing.
"""

import subprocess
import sys
import textwrap

import pytest

# Every module that reaches litellm through the proxy. The list is the point: one
# module-scope `import litellm` anywhere in the API's import graph undoes the whole
# thing, so all of them are checked rather than a representative one.
PROXY_USERS = [
    "matrix_studio.engine.simulator",
    "matrix_studio.naming",
    "matrix_studio.pressure",
    "matrix_studio.validation",
    "matrix_studio.analysis",
    "matrix_studio.persona_wizard",
]


def _in_subprocess(code: str) -> str:
    """Run code in a fresh interpreter.

    Required: this test suite imports litellm through other tests, so
    ``"litellm" in sys.modules`` is already true in-process and an in-process
    assertion would pass no matter what the source said.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# It stays deferred
# --------------------------------------------------------------------------- #


def test_importing_the_api_app_does_not_import_litellm():
    """The cold-start guard. This is the assertion that keeps init under 10 s."""
    out = _in_subprocess(
        """
        import sys
        from matrix_studio.api.app import create_app
        create_app()
        print("litellm" in sys.modules, "openai" in sys.modules)
        """
    )
    assert out == "False False", (
        f"litellm/openai were imported building the app ({out}). Something in the "
        "API's import graph imports litellm at module scope again; the deployed "
        "cold start regresses to ~5.7 s and can trip Lambda's 10 s init limit."
    )


@pytest.mark.parametrize("module", PROXY_USERS)
def test_importing_a_generating_module_does_not_import_litellm(module):
    out = _in_subprocess(
        f"""
        import sys, importlib
        importlib.import_module({module!r})
        print("litellm" in sys.modules)
        """
    )
    assert out == "False", f"{module} imports litellm at module scope"


def test_the_deferred_import_is_actually_faster():
    """Guards the *reason* for the indirection, not just its shape.

    A proxy that resolved eagerly would satisfy every other test here while
    restoring the cold start. Timing is the only thing that catches that.
    """
    out = _in_subprocess(
        """
        import time
        t0 = time.perf_counter()
        from matrix_studio.api.app import create_app
        create_app()
        print(f"{time.perf_counter() - t0:.3f}")
        """
    )
    elapsed = float(out)
    # Measured 0.24 s after the change, 1.91 s before. 1.0 s sits well clear of
    # both, so this fails on a real regression without flaking on a slow host.
    assert elapsed < 1.0, (
        f"building the app took {elapsed:.2f}s; it was 0.24s when the litellm "
        "import was deferred and 1.91s before. Cold start has regressed."
    )


# --------------------------------------------------------------------------- #
# It stays patchable — the seam 132 existing tests use
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", PROXY_USERS)
def test_the_module_attribute_is_still_patchable(module):
    """``patch("matrix_studio.x.litellm.acompletion")`` must keep working.

    A function-local ``import litellm`` would also defer the cost and would break
    every one of those patches on the attribute lookup — silently turning tests
    that mean to stub the provider into tests that would call it.
    """
    import importlib
    from unittest.mock import patch

    target = importlib.import_module(module)
    # Identity, not a call: `acompletion` is async, so `patch` installs an
    # AsyncMock and calling it would only prove a coroutine came back.
    with patch(f"{module}.litellm.acompletion") as mocked:
        assert target.litellm.acompletion is mocked

    # And restored afterwards, on the real module, not left holding the mock.
    import litellm

    assert not hasattr(litellm.acompletion, "return_value")
    assert target.litellm.acompletion is litellm.acompletion


def test_patching_through_one_module_is_visible_through_another():
    """litellm is a singleton module, and the proxy must not change that.

    Tests patch ``matrix_studio.engine.simulator.litellm.acompletion`` and exercise
    code paths in `pressure` and `validation` that call it. If the proxy held
    per-module state those patches would stop taking effect.
    """
    from unittest.mock import patch

    from matrix_studio import pressure
    from matrix_studio.engine import simulator

    with patch("matrix_studio.engine.simulator.litellm.acompletion") as mocked:
        assert pressure.litellm.acompletion is mocked
        assert simulator.litellm.acompletion is mocked


# --------------------------------------------------------------------------- #
# The settings that used to depend on import order
# --------------------------------------------------------------------------- #


def test_resolving_applies_drop_params_and_suppress_debug_info():
    """Without ``drop_params`` the engine writes provider errors into the
    transcript as the character's speech. It has to be set before any call."""
    out = _in_subprocess(
        """
        import sys
        from matrix_studio.lazy_litellm import litellm
        print("litellm" in sys.modules, end=" ")
        litellm.acompletion            # first touch resolves the module
        print(sys.modules["litellm"].drop_params,
              sys.modules["litellm"].suppress_debug_info)
        """
    )
    assert out == "False True True"


@pytest.mark.parametrize("module", PROXY_USERS)
def test_every_entry_point_configures_litellm_not_just_the_simulator(module):
    """The order-dependence this replaced.

    ``drop_params`` used to be set at ``simulator.py`` import time, so a process
    that imported only ``naming`` or ``analysis`` — which both call
    ``acompletion`` — ran without it and hit ``UnsupportedParamsError`` on the
    deployed Bedrock model.
    """
    out = _in_subprocess(
        f"""
        import sys, importlib
        module = importlib.import_module({module!r})
        module.litellm.acompletion
        print(sys.modules["litellm"].drop_params)
        """
    )
    assert out == "True"

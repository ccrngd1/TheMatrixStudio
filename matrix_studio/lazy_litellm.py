# SPDX-License-Identifier: Apache-2.0
"""
``litellm``, imported on first use rather than at module import.

## Why

Measured 2026-09-12 on the deployed API Lambda: importing ``matrix_studio.api.app``
costs 1.91 s, of which **litellm is 1.70 s** — it drags in the whole ``openai``
package and a several-hundred-module type tree. On Lambda that inflated the init
phase to 5.7 s, and Lambda's init limit is a hard **10 seconds**. When init
overruns, Lambda aborts and retries it, so a single cold start became:

    INIT_REPORT Init Duration: 9999.60 ms   Phase: init  Status: timeout
    INIT_REPORT Init Duration: 10000.14 ms  Phase: init  Status: timeout
    GET /api/runs 200
    REPORT Duration: 24761.26 ms

Twenty of those twenty-five seconds bought nothing. The user saw the History
view's "Loading…" and reasonably concluded the app was broken.

The routes that need an LLM are the ones that generate — turns, personas, names,
summaries — and they take seconds anyway, so paying 1.7 s on the first of them is
invisible. The routes that do not (``/api/runs``, ``/api/health``, every read) now
pay nothing.

## Why a proxy rather than moving the import into each function

Both would defer the cost, but 132 tests patch through the *module attribute*:

    patch("matrix_studio.engine.simulator.litellm.acompletion")

A function-local ``import litellm`` deletes ``simulator.litellm``, and every one of
those patches fails on the attribute lookup. This proxy keeps the name bound at
module scope, so the seam is untouched: ``mock`` reads ``proxy.acompletion``
(resolving the real module), then sets the attribute on the real module exactly as
it does today.

## What it also fixes

``suppress_debug_info`` and ``drop_params`` used to be set at ``simulator.py``
import time, which made them depend on *import order*: ``naming.py``,
``analysis.py`` and the rest call ``acompletion`` and would run without
``drop_params`` in any process that never imported the simulator. Bedrock's
``global.anthropic.claude-sonnet-5`` accepts only ``temperature=1``, so an
unconfigured call raises ``UnsupportedParamsError`` — which the engine used to
write into the transcript *as the character's speech*. Applying the settings at
resolution makes that unskippable: nothing can reach ``acompletion`` through this
proxy without them.
"""

from types import ModuleType


class _LazyLiteLLM:
    """Forwards every attribute to ``litellm``, importing it on first touch."""

    __slots__ = ("_module",)

    def __init__(self) -> None:
        # `object.__setattr__` because this class's own `__setattr__` forwards to
        # the real module, which does not exist yet.
        object.__setattr__(self, "_module", None)

    def _resolve(self) -> ModuleType:
        module = object.__getattribute__(self, "_module")
        if module is None:
            import litellm as real

            # Configure at resolution, not at some module's import: see the
            # docstring. Idempotent, and cannot be reached around.
            real.suppress_debug_info = True

            # Drop provider-unsupported sampling params instead of erroring.
            #
            # Measured 2026-09-06: `bedrock/global.anthropic.claude-sonnet-5`
            # accepts ONLY temperature=1, so every call raised
            # UnsupportedParamsError and the engine wrote the error text into the
            # transcript AS THE CHARACTER'S SPEECH:
            #
            #   "[Error generating response: litellm.UnsupportedParamsError: ...
            #    does not support temperature=0.7. Only temperature=1 is
            #    supported.]"
            #
            # The engine passes temperature from settings (0.7), 0.3 for speaker
            # selection and 0.0 for the validation gate and reflection, so a model
            # with parameter restrictions failed on every path at once.
            # "Provider-agnostic" is a stated project goal (PROJECT-SPEC §7);
            # assuming every model accepts our sampling params is not that.
            #
            # Dropping is the right trade: a slightly different temperature is a
            # far smaller loss than a run of error strings, and the alternative —
            # per-model capability tables in this codebase — is exactly the
            # provider coupling LiteLLM exists to avoid.
            real.drop_params = True

            object.__setattr__(self, "_module", real)
            module = real
        return module

    def __getattr__(self, name: str):
        # Reached only for names that are not slots or class attributes, so
        # `_module` and `_resolve` never come through here.
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value) -> None:
        # `mock.patch` sets the attribute on whatever this resolves to, so it must
        # land on the real module — otherwise a patch would be silently discarded
        # and the test would exercise the live provider.
        setattr(self._resolve(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._resolve(), name)

    def __dir__(self):
        return dir(self._resolve())

    def __repr__(self) -> str:
        loaded = object.__getattribute__(self, "_module") is not None
        return f"<lazy litellm ({'loaded' if loaded else 'not yet imported'})>"


litellm = _LazyLiteLLM()
"""Import this instead of ``litellm`` itself.

    from matrix_studio.lazy_litellm import litellm

Call sites are unchanged — ``await litellm.acompletion(...)`` still reads the same
and still resolves to the real function.
"""

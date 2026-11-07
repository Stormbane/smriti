"""``test_stub``: a canned answer for tests and CI's smoke run, never a real night.

It answers every request at once with a short text that names what it
was asked, without a model or a network, so CI can run the nightly end to
end where no LLM is installed. It only runs where ``SMRITI_ALLOW_TEST_STUB=1``
says this is a test: on a real install, a stray
``SMRITI_NIGHTLY_PROVIDER=test_stub`` fails the night (the provider
raises :class:`LLMError`, which the nightly records), where it would
otherwise write canned digests into the memory tree. It is never
auto-detected and never listed among the providers.
"""

from __future__ import annotations

import os

from smriti.llm.types import LLMError, LLMRequest, LLMResponse

NAME = "test_stub"
ALLOW_ENV = "SMRITI_ALLOW_TEST_STUB"


def allowed() -> bool:
    """Whether this process says it is a test (``SMRITI_ALLOW_TEST_STUB=1``)."""
    return os.environ.get(ALLOW_ENV, "").strip() == "1"


def _refuse() -> LLMError:
    return LLMError(f"the {NAME} LLM gives canned answers for tests; it runs only "
                    f"with {ALLOW_ENV}=1")


class StubLLMProvider:
    """Canned answers (see the module docstring); refused outside a test."""

    name = NAME

    def __init__(self) -> None:
        if not allowed():
            raise _refuse()

    def is_available(self) -> bool:
        return allowed()

    def default_model(self, role: str = "executor") -> str:
        return NAME

    def call(self, request: LLMRequest) -> LLMResponse:
        if not allowed():  # the environment can change after the first call
            raise _refuse()
        prompt = "\n".join(m.content for m in request.turns())
        first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
        text = (f"## Highlights\n- {NAME}: a canned answer to a prompt of {len(prompt):,} "
                f"characters, which began: {first[:80]}")
        return LLMResponse(text=text, model=NAME, tokens_in=len(prompt) // 4,
                           tokens_out=len(text) // 4)

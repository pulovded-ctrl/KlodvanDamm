"""Live-trading gate. Three independent locks must all be open."""

from __future__ import annotations

LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"
LIVE_ENV_VAR = "LIVE_TRADING"


def live_gate_error(*, flag: bool, env_value: str | None, typed_phrase: str | None) -> str | None:
    """Returns a human-readable reason why live trading is NOT allowed, or None."""
    if not flag:
        return "the --live flag is missing"
    if (env_value or "").strip().lower() != "true":
        return f"environment variable {LIVE_ENV_VAR} is not 'true'"
    if typed_phrase is None:
        return "the confirmation phrase was not typed"
    if typed_phrase.strip() != LIVE_CONFIRMATION_PHRASE:
        return "the confirmation phrase did not match"
    return None

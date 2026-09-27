"""Live-trading gate. Three independent locks must all be open."""

from __future__ import annotations

LIVE_CONFIRMATION_PHRASE = "ВКЛЮЧАЮ РЕАЛЬНУЮ ТОРГОВЛЮ"
LIVE_ENV_VAR = "LIVE_TRADING"


def live_gate_error(*, flag: bool, env_value: str | None, typed_phrase: str | None) -> str | None:
    """Returns a human-readable (Russian) reason why live trading is NOT allowed, or None."""
    if not flag:
        return "нет флага --live"
    if (env_value or "").strip().lower() != "true":
        return f"переменная {LIVE_ENV_VAR} не равна true"
    if typed_phrase is None:
        return "подтверждающая фраза не введена"
    if typed_phrase.strip() != LIVE_CONFIRMATION_PHRASE:
        return "подтверждающая фраза не совпала"
    return None

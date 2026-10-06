"""Named strategy hypotheses with explicit defaults, never live parameter fitting."""
from core.settings import cfg


def strategy_settings(settings=None):
    original = settings or cfg
    effective = original.model_copy(deep=True)
    if original.strategy_profile in ("balanced", "contextual"):
        effective.fvg_confirmations = 1
        # Keep the user's risk cap, volume and score thresholds unchanged.
    return effective

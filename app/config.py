import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # Temporarily switched from Haiku to Sonnet as a live trial, at
    # Александр's explicit request (2026-09-16) — now that checks turned
    # out cheap overall (a few cents each) and "Срочно" defaults to on, he
    # wants to try whether Sonnet's extra judgment is worth roughly 3x the
    # per-token price (see claude_client.MODEL_PRICING_PER_TOKEN) before
    # settling on a default. Override via the CLAUDE_MODEL env var on
    # Railway if needed; revert this line to
    # "claude-haiku-4-5-20251001" to go back to the cheaper default.
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
    # A short list of languages (see app.claude_client.HARD_LANGUAGE_BASES)
    # get this stronger, ~3x-the-price model instead — agreed with
    # Александр after costing it out: kazakh/kyrgyz/tajik/uzbek/swahili/
    # telugu/marathi/azerbaijani are less common in the base model's
    # training data, so the extra reliability is worth the modest total
    # cost impact (only these languages' calls use it, not the whole job).
    # Currently the same model as CLAUDE_MODEL above during the Sonnet
    # trial, so this distinction is a no-op for now — it'll matter again
    # the moment CLAUDE_MODEL reverts to Haiku.
    CLAUDE_MODEL_HARD: str = os.environ.get("CLAUDE_MODEL_HARD", "claude-sonnet-4-5-20250929")
    ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")


settings = Settings()

if not settings.ANTHROPIC_API_KEY:
    # AI-based checks (register/typo/untranslatable/completeness) are
    # silently skipped when no key is configured — rule-based checks
    # (numbers/placeholders/length) still work. This lets the app boot
    # locally without a key; Railway must have ANTHROPIC_API_KEY set for
    # AI checks to actually run.
    import warnings

    warnings.warn("ANTHROPIC_API_KEY is not set — AI-based checks will be skipped.")

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # Haiku is plenty for this kind of structured, criteria-based QA check
    # (it's classification against clear rules, not open-ended reasoning) and
    # costs roughly half of Sonnet per token on both input and output — a
    # meaningful saving when a single multi-language upload can mean dozens
    # of API calls (one per language). Override via the CLAUDE_MODEL env var
    # on Railway if a particular project needs Sonnet's extra judgment.
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")


settings = Settings()

if not settings.ANTHROPIC_API_KEY:
    # AI-based checks (glossary/register/typo) are silently skipped when no
    # key is configured — rule-based checks (numbers/placeholders/length)
    # still work. This lets the app boot locally without a key; Railway
    # must have ANTHROPIC_API_KEY set for AI checks to actually run.
    import warnings

    warnings.warn("ANTHROPIC_API_KEY is not set — AI-based checks will be skipped.")

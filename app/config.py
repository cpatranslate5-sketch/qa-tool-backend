import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")


settings = Settings()

if not settings.ANTHROPIC_API_KEY:
    # AI-based checks (glossary/register/typo) are silently skipped when no
    # key is configured — rule-based checks (numbers/placeholders/length)
    # still work. This lets the app boot locally without a key; Railway
    # must have ANTHROPIC_API_KEY set for AI checks to actually run.
    import warnings

    warnings.warn("ANTHROPIC_API_KEY is not set — AI-based checks will be skipped.")

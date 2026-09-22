import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings:
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # Temporarily switched from Haiku to Sonnet as a live trial, at
    # Александр's explicit request (2026-09-16) — checks turned out cheap
    # overall (a few cents each), so he wanted to try whether Sonnet's
    # extra judgment is worth roughly 3x the per-token price (see
    # claude_client.MODEL_PRICING_PER_TOKEN) before settling on a default.
    # ("Срочно" defaulted to ON at the time this was written — it's back
    # to OFF by default since 2026-09-18, see CheckRunner.tsx's `urgent`
    # state on the frontend, as part of the same cost-cutting pass that
    # moved HARD_LANGUAGE_BASES to Opus below.) Override via the
    # CLAUDE_MODEL env var on Railway if needed; revert this line to
    # "claude-haiku-4-5-20251001" to go back to the cheaper default.
    CLAUDE_MODEL: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
    # A short list of languages (see app.claude_client.HARD_LANGUAGE_BASES)
    # get this stronger model instead. Switched to Opus 5 on 2026-09-18 —
    # Александр compared real Opus vs Sonnet reports side by side for
    # several of these languages and found Opus genuinely caught more real
    # mistakes, so he wants it specifically where it matters most, while
    # affording the ~2.5x-the-price jump by keeping it OFF the list for
    # every other language (those stay on CLAUDE_MODEL/Sonnet) and by
    # shortening the AI's own written answers (see claude_client's
    # _CONCISENESS_INSTRUCTION and the register-check trimming) — output
    # tokens are the expensive side of the bill. Override via the
    # CLAUDE_MODEL_HARD env var on Railway if a different model is ever
    # needed there without a code change — IMPORTANT: if Railway already
    # has this env var set from an earlier trial (e.g. to a Sonnet id),
    # that value wins over this default and must be updated there too.
    CLAUDE_MODEL_HARD: str = os.environ.get("CLAUDE_MODEL_HARD", "claude-opus-5")
    ALLOWED_ORIGINS: list[str] = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

    # Второй ИИ для сравнения ("🌐 Проверить также через Gemini" — see
    # app.gemini_client) — Александр's ask, 2026-09-22, after a blind test
    # showed Gemini independently caught a Marathi meaning error that even
    # Opus with a loosened confidence bar completely missed (see
    # app.excel_multi.gemini_check's own comment for the full story). A
    # SEPARATE Google AI Studio API key, not the Anthropic one above — get
    # one at aistudio.google.com ("Get API key"), then set GEMINI_API_KEY on
    # Railway. Optional: GEMINI_MODEL overrides the model id below if
    # Google renames/retires this one later (see gemini_client.py's own
    # comment on why this is worth keeping easy to change).
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")


settings = Settings()

if not settings.ANTHROPIC_API_KEY:
    # AI-based checks (register/typo/untranslatable/completeness) are
    # silently skipped when no key is configured — rule-based checks
    # (numbers/placeholders/length) still work. This lets the app boot
    # locally without a key; Railway must have ANTHROPIC_API_KEY set for
    # AI checks to actually run.
    import warnings

    warnings.warn("ANTHROPIC_API_KEY is not set — AI-based checks will be skipped.")

if not settings.GEMINI_API_KEY:
    # Only the opt-in "🌐 Проверить также через Gemini" pass is skipped —
    # everything else (the whole rest of the product) works exactly as
    # before without this key, same graceful-degradation pattern as above.
    import warnings

    warnings.warn("GEMINI_API_KEY is not set — the optional Gemini second-opinion check will be skipped.")

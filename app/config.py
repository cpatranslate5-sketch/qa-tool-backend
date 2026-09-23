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
    # Added 2026-09-23 (Александр's ask): a GPT model joins Sonnet on Step 1
    # of the search (see claude_client._ensemble_search_findings) for model-
    # family diversity — Kyrgyz detection kept feeling inconsistent even on
    # the byte-identical, already-proven plain Sonnet+Sonnet pipeline, most
    # likely ordinary run-to-run LLM variance rather than a code regression.
    # A model from a different vendor is less likely to share Claude's own
    # blind spots than another Claude model would (this is also why the
    # earlier Sonnet+Haiku ensemble — same vendor — was reverted). Left
    # EMPTY by default: the GPT branch silently contributes nothing when no
    # key is set, exactly like ANTHROPIC_API_KEY's own missing-key
    # behavior, so nothing breaks before a real key is added on Railway.
    # Get a key from platform.openai.com.
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    # "mini" = OpenAI's cheap/fast tier — Александр's explicit request for
    # "простую версию" (a simple version), mirroring Haiku's old role.
    # $0.25/$2.00 per million input/output tokens, confirmed against
    # OpenAI's own GPT-5-for-developers pricing announcement (checked
    # 2026-09-23) — see claude_client.OPENAI_MODEL_PRICING_PER_TOKEN.
    # Override via the OPENAI_MODEL env var on Railway if OpenAI's
    # naming/pricing moves on.
    OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
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

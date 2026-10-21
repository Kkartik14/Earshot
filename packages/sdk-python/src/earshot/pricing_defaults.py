"""Built-in prices: dated, **regular** list prices for the first providers (FR-4, AC4.2).

Each row is the provider's published regular list price on its source page as of
``AS_OF``, never a promotional, plan-discounted or trial price, and carries that page
and date. A model whose price is not public (Groq's Llama models: "Contact Sales")
has no row and prices as ``no_price``. Rows are effective from 0: they price every
call until a later row (a project's own, or an operator's ``earshot prices add
--builtin``) replaces them.

Provider names are the OpenTelemetry GenAI registry spellings usage is normalized to
(``call_span_facts.provider_name``); models are the API model IDs, lower-cased, and
match exactly. A price is ``price_nano_usd`` (1e-9 USD) per ``per_quantity`` units:
``$0.075 / 1M tokens`` is ``75`` per ``1``; ``$0.0077 / minute`` is ``7_700_000`` per
``60`` audio seconds.

Choices the source pages leave open, stated in each row's note:

- **Deepgram Nova-3 streaming** lists a "limited-time promotional" PAYG price
  ($0.0048/min); the regular price ($0.0077/min, monolingual) is the row. Its
  general model is ``nova-3`` (LiveKit) or ``nova-3-general`` (Pipecat).
- **Cartesia** sells characters by plan only; the row is the Scale plan's overage
  rate, $38 per 1M characters (Pro $65, Startup $45).
- **Groq Whisper** bills at least 10 s per request; the row is the hourly rate and
  does not model the minimum.

Source: ``local/research/month-2/livekit-ts-sdk-cost.md`` §6.5 (fetched 2026-10-19).
"""

from __future__ import annotations

from .storage_prices import NewPrice

AS_OF = "2026-10-19"
_DEEPGRAM = "https://deepgram.com/pricing"
_CARTESIA = "https://cartesia.ai/pricing"
_GROQ_MODELS = "https://console.groq.com/docs/models"
_GROQ_STT = "https://console.groq.com/docs/speech-to-text"
_OPENAI = "https://developers.openai.com/api/docs/pricing"
_ELEVENLABS = "https://elevenlabs.io/pricing/api"


def _row(
    provider: str,
    model: str,
    unit: str,
    price_nano_usd: int,
    per_quantity: int,
    source_url: str,
    note: str = "",
) -> NewPrice:
    return NewPrice(
        provider=provider,
        model=model,
        unit=unit,
        price_nano_usd=price_nano_usd,
        per_quantity=per_quantity,
        effective_from_unix_nano=0,
        source_url=source_url,
        note=f"regular list price as of {AS_OF}" + (f"; {note}" if note else ""),
    )


def _tokens(
    provider: str, model: str, source_url: str, per_million: tuple[float, ...]
) -> list[NewPrice]:
    """Input, (cached input,) output token rows from USD per 1M tokens."""

    units = (
        ("input_token", "output_token")
        if len(per_million) == 2
        else ("input_token", "cached_input_token", "output_token")
    )
    return [
        _row(provider, model, unit, round(usd * 1000), 1, source_url)
        for unit, usd in zip(units, per_million, strict=True)
    ]


_STT_NOVA3 = "streaming, monolingual; the promotional $0.0048/min is not used"
BUILTIN_PRICES: tuple[NewPrice, ...] = (
    # STT, per audio second.
    _row("deepgram", "nova-3", "audio_second", 7_700_000, 60, _DEEPGRAM, _STT_NOVA3),
    _row("deepgram", "nova-3-general", "audio_second", 7_700_000, 60, _DEEPGRAM, _STT_NOVA3),
    _row("openai", "whisper-1", "audio_second", 6_000_000, 60, _OPENAI),
    _row(
        "groq",
        "whisper-large-v3",
        "audio_second",
        111_000_000,
        3600,
        _GROQ_STT,
        "billed at least 10 s per request; not modelled",
    ),
    _row(
        "groq",
        "whisper-large-v3-turbo",
        "audio_second",
        40_000_000,
        3600,
        _GROQ_STT,
        "billed at least 10 s per request; not modelled",
    ),
    # TTS, per character.
    _row(
        "cartesia",
        "sonic-3",
        "character",
        38_000,
        1,
        _CARTESIA,
        "Scale plan overage ($38 per 1M characters); plan-based",
    ),
    _row(
        "cartesia",
        "sonic-2",
        "character",
        38_000,
        1,
        _CARTESIA,
        "Scale plan overage ($38 per 1M characters); plan-based",
    ),
    _row("openai", "tts-1", "character", 15_000, 1, _OPENAI),
    _row("openai", "tts-1-hd", "character", 30_000, 1, _OPENAI),
    _row("elevenlabs", "eleven_flash_v2_5", "character", 40_000, 1, _ELEVENLABS),
    _row("elevenlabs", "eleven_turbo_v2_5", "character", 40_000, 1, _ELEVENLABS),
    _row("elevenlabs", "eleven_multilingual_v2", "character", 80_000, 1, _ELEVENLABS),
    _row("elevenlabs", "eleven_v3", "character", 80_000, 1, _ELEVENLABS),
    # LLM, per token (USD per 1M: input, [cached input,] output).
    *_tokens("groq", "openai/gpt-oss-20b", _GROQ_MODELS, (0.075, 0.30)),
    *_tokens("groq", "openai/gpt-oss-120b", _GROQ_MODELS, (0.15, 0.60)),
    *_tokens("openai", "gpt-4o", _OPENAI, (2.50, 1.25, 10.00)),
    *_tokens("openai", "gpt-4o-mini", _OPENAI, (0.15, 0.075, 0.60)),
    *_tokens("openai", "gpt-4.1", _OPENAI, (2.00, 0.50, 8.00)),
    *_tokens("openai", "gpt-4.1-mini", _OPENAI, (0.40, 0.10, 1.60)),
    *_tokens("openai", "gpt-4.1-nano", _OPENAI, (0.10, 0.025, 0.40)),
)

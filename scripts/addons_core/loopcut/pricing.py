"""One price table for the add-on's job budgets and the eval reports.

Prices are USD per million tokens: (uncached input, cached input, output[, cache write]). `usage` is the
shape llm.Completion.usage uses: {"input": prompt tokens including cached, "cached": ..., "output": ...},
plus "cache_write" from providers that bill writing the cache (Anthropic: 1.25x input for the 5-minute
cache), priced at the fourth figure, else as uncached input; other keys (a role bucket's "requests") are
ignored. Sources: the 2026-09-21 model-tiers analysis and the gateway's model catalogue (loopcut-web
supabase/migrations), which uses the Muse ids below; Anthropic's from platform.claude.com/docs/en/about-claude/pricing.
"""

_GLM_FLASH = (0.15, 0.03, 0.50)
_MUSE_STANDARD = (1.25, 0.15, 4.25)
_MUSE_CONTRIBUTOR = (0.10, 0.002, 0.20)   # trains on user data
# Anthropic list prices, checked 2026-10-10: input, cache read, output, 5-minute cache write. Long prompts cost
# no more, except on Haiku 5.5 (LONG_CONTEXT). The eval's cc arm reports Opus as a reference only.
_OPUS_5_5 = (4.00, 0.20, 20.00, 5.00)
_SONNET_5_5 = (2.00, 0.10, 10.00, 2.50)
_HAIKU_5_5 = (0.10, 0.01, 0.50, 0.125)
_FABLE_5_1 = (10.00, 0.25, 50.00, 12.50)
_GPT_6_1_SOL = (2.00, 0.10, 10.00)  # OpenAI list price at launch, 2026-09-29 (OpenRouter matches)

PRICES = {
    "glm-5.3-flash": _GLM_FLASH,
    "fast": _GLM_FLASH,                             # service alias for GLM-5.3-Flash
    "muse-spark-standard": _MUSE_STANDARD,
    "muse-spark-contributor": _MUSE_CONTRIBUTOR,
    "muse-spark-1.3": _MUSE_STANDARD,               # gateway: Meta direct, standard tier
    "muse-spark-1.3-contributor": _MUSE_CONTRIBUTOR,  # gateway: Meta direct, contributor tier
    "meta/muse-spark-1.3-contributor": _MUSE_CONTRIBUTOR,  # gateway: OpenRouter, contributor tier
    "pro": _MUSE_STANDARD,                          # service alias, priced at Muse Standard
    "claude-opus-5-5": _OPUS_5_5,
    "claude-opus-5.5": _OPUS_5_5,
    "anthropic/claude-opus-5.5": _OPUS_5_5,         # gateway: OpenRouter
    "opus": _OPUS_5_5,                              # service alias for Claude Opus 5.5
    "claude-sonnet-5-5": _SONNET_5_5,
    "claude-haiku-5-5": _HAIKU_5_5,
    "claude-fable-5-1": _FABLE_5_1,
    "gpt-6.1-sol": _GPT_6_1_SOL,
    "openai/gpt-6.1-sol": _GPT_6_1_SOL,             # OpenRouter
}


# A prompt over this many tokens (cache reads and writes included) is priced at the second figures instead.
LONG_CONTEXT = {"claude-haiku-5-5": (100_000, (0.50, 0.05, 2.50, 0.625))}


def usd(model, usage):
    """Dollars for one usage dict, or None when the model has no price."""
    price = PRICES.get((model or "").lower())
    if price is None or not usage:
        return None
    total = int(usage.get("input") or 0)
    long = LONG_CONTEXT.get((model or "").lower())
    if long and total > long[0]:
        price = long[1]
    cached = min(int(usage.get("cached") or 0), total)
    written = min(int(usage.get("cache_write") or 0), total - cached)
    write_price = price[3] if len(price) > 3 else price[0]
    return ((total - cached - written) * price[0] + cached * price[1] + written * write_price
            + int(usage.get("output") or 0) * price[2]) / 1e6

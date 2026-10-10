"""One price table for the add-on's job budgets and the eval reports.

Prices are USD per million tokens: (uncached input, cached input, output). `usage` is the shape
llm.Completion.usage uses: {"input": prompt tokens including cached, "cached": ..., "output": ...};
other keys (a role bucket's "requests") are ignored. Sources: the 2026-09-21 model-tiers analysis and
the gateway's model catalogue (loopcut-web supabase/migrations), which uses the Muse ids below.
"""

_GLM_FLASH = (0.15, 0.03, 0.50)
_MUSE_STANDARD = (1.25, 0.15, 4.25)
_MUSE_CONTRIBUTOR = (0.10, 0.002, 0.20)   # trains on user data
_OPUS_5_5 = (4.00, 0.20, 20.00)           # list price; the eval's cc arm reports it as a reference only
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
    "gpt-6.1-sol": _GPT_6_1_SOL,
    "openai/gpt-6.1-sol": _GPT_6_1_SOL,             # OpenRouter
}


def usd(model, usage):
    """Dollars for one usage dict, or None when the model has no price."""
    price = PRICES.get((model or "").lower())
    if price is None or not usage:
        return None
    total = int(usage.get("input") or 0)
    cached = min(int(usage.get("cached") or 0), total)
    return ((total - cached) * price[0] + cached * price[1] + int(usage.get("output") or 0) * price[2]) / 1e6

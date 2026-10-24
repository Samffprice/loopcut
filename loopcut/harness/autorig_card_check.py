"""Assert the owner's copy budget against actual rendered rig-card primitives."""
import re

FORBIDDEN = re.compile(r'\b(?:runpod|gpus?|workers?|storage|sha-?256)\b|upload(?:ed)? hashes?', re.I)


def check(prims, prefix):
    text = ' '.join(p.get('text', '') for p in prims if p.get('id', '').startswith(prefix))
    # Decimal prices and domain names are not sentence boundaries.
    sentences = re.findall(r'[.!?](?=\s|$)', re.sub(r'https?://\S+', '', text))
    if len(sentences) > 2 or FORBIDDEN.search(text):
        raise AssertionError(f'Rig card exceeds copy budget: {text}')
    return text

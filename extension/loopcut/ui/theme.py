"""Colors (sRGB, 0-1 RGBA) and metrics (unscaled pixels; layout multiplies by the UI scale)."""


def _hex(value: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    value = value.lstrip("#")
    return (int(value[0:2], 16) / 255, int(value[2:4], 16) / 255, int(value[4:6], 16) / 255, alpha)


BG = _hex("#181818")
HEADER_BG = _hex("#181818")
CARD = _hex("#212121")
CARD_BORDER = _hex("#333333")
CODE_BG = _hex("#141414")
INPUT_BG = _hex("#1f1f1f")
INPUT_BORDER = _hex("#3a3a3a")
ACCENT = _hex("#4d9cf6")
SELECTION = _hex("#264f78")
TEXT = _hex("#d6d6d6")
TEXT_MUTED = _hex("#858585")
TEXT_FAINT = _hex("#5c5c5c")
CODE_TEXT = _hex("#c5c8c6")
OK = _hex("#56b97f")
WARN = _hex("#d9a441")
ERROR = _hex("#e5646a")
ERROR_BG = _hex("#2a1b1c")
ERROR_BORDER = _hex("#5a2e31")
BUTTON = _hex("#2d6fcf")
BUTTON_TEXT = _hex("#ffffff")
BUTTON_GHOST = _hex("#2a2a2a")
NONE = (0.0, 0.0, 0.0, 0.0)

FONT_SIZE = 13
FONT_SIZE_SMALL = 11
FONT_SIZE_CODE = 12
LINE_HEIGHT = 1.5        # Multiplier on font size.
PAD = 12                 # Panel gutter.
CARD_PAD = 10
RADIUS = 8
RADIUS_SMALL = 5
GAP = 10                 # Between chat items.
HEADER_HEIGHT = 34
CODE_PREVIEW_LINES = 10
INPUT_MIN_LINES = 2
INPUT_MAX_LINES = 8

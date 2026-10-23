"""UI language: Korean source strings looked up in per-language tables.

The Korean text in the code is the message key (gettext style), so the code
reads as before: t("정지됨") gives "Stopped" in English. Placeholders use
str.format names: t("변속 {gear}단", gear=3). N_() only marks a string kept
in a table for later t(). A missing translation falls back to English, then
to the Korean key. The tables live in locales/<code>.py.

  python i18n.py      list missing, unused and mismatched translations
"""

from __future__ import annotations

import ctypes

from locales import de, en, es, fr, ja, pl, pt, ru, zh

# code, name in that language; the order of the language menu
LANGUAGES = (
    ("ko", "한국어"),
    ("en", "English"),
    ("ja", "日本語"),
    ("zh", "简体中文"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("es", "Español"),
    ("pt", "Português (Brasil)"),
    ("pl", "Polski"),
    ("ru", "Русский"),
)
TABLES = {"en": en.MESSAGES, "ja": ja.MESSAGES, "zh": zh.MESSAGES, "de": de.MESSAGES, "fr": fr.MESSAGES,
          "es": es.MESSAGES, "pt": pt.MESSAGES, "pl": pl.MESSAGES, "ru": ru.MESSAGES}
FONTS = {"ko": "Malgun Gothic", "ja": "Yu Gothic UI", "zh": "Microsoft YaHei UI"}
LATIN_FONT = "Segoe UI"
# Windows primary language id -> code
WINDOWS_LANGUAGES = {0x12: "ko", 0x09: "en", 0x11: "ja", 0x04: "zh", 0x07: "de", 0x0C: "fr", 0x0A: "es",
                     0x16: "pt", 0x15: "pl", 0x19: "ru"}

_current = "ko"


def N_(text: str) -> str:
    return text


def t(text: str, **values: object) -> str:
    message = text if _current == "ko" else TABLES[_current].get(text) or en.MESSAGES.get(text) or text
    return message.format(**values) if values else message


def language() -> str:
    return _current


def set_language(code: str) -> None:
    global _current
    _current = code if code in dict(LANGUAGES) else "en"


def system_language() -> str:
    """The Windows display language if there is a table for it, else English."""
    try:
        primary = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except (AttributeError, OSError):
        return "en"
    return WINDOWS_LANGUAGES.get(primary, "en")


def ui_font() -> str:
    return FONTS.get(_current, LATIN_FONT)


def _check() -> int:
    """Compare the t()/N_() strings in the code with every table."""
    import ast
    import string
    from pathlib import Path

    def fields(text: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    keys: dict[str, str] = {}
    for path in sorted(Path(__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("t", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.setdefault(node.args[0].value, f"{path.name}:{node.lineno}")
    problems = 0
    for code, table in TABLES.items():
        for key, where in keys.items():
            if key not in table:
                print(f"{code}: missing {key!r} ({where})")
                problems += 1
            elif fields(table[key]) != fields(key):
                print(f"{code}: placeholders differ for {key!r}: {table[key]!r}")
                problems += 1
        for key in table.keys() - keys.keys():
            print(f"{code}: unused {key!r}")
            problems += 1
    print(f"{len(keys)} strings, {len(TABLES)} languages, {problems} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(_check())

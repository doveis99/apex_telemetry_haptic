r"""Write the window's translation tables (python/locales/*.py) for the Rust app.

  .venv\Scripts\python.exe tools\export_locales.py

Two forms of the same tables, both keyed by the Korean source text as i18n.py is:
  resources/locales/<code>.json                   embedded by src/i18n.rs, for t!() in Rust code
  resources/lang/<code>/LC_MESSAGES/ats_haptics.po  bundled by slint-build, for @tr() in ui/*.slint

A message missing from a language gets the English one, as i18n.t falls back to it. Run
this again whenever a table changes; the tests check that every message the Rust code and
the .slint files ask for has an English entry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import i18n  # noqa: E402


def po_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def main() -> None:
    out = ROOT / "resources" / "locales"
    out.mkdir(exist_ok=True)
    english = i18n.TABLES["en"]
    keys = sorted(set().union(*i18n.TABLES.values()))
    for code, table in i18n.TABLES.items():
        path = out / f"{code}.json"
        path.write_text(json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True) + "\n", encoding="utf-8",
                        newline="\n")
        po = ROOT / "resources" / "lang" / code / "LC_MESSAGES" / "ats_haptics.po"
        po.parent.mkdir(parents=True, exist_ok=True)
        lines = ['msgid ""', 'msgstr ""', po_string("Content-Type: text/plain; charset=UTF-8\n"),
                 po_string(f"Language: {code}\n"), ""]
        for key in keys:
            lines += [f"msgid {po_string(key)}", f"msgstr {po_string(table.get(key) or english.get(key) or key)}", ""]
        po.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        print(f"{code}: {len(table)} messages -> {path.relative_to(ROOT)}, {po.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

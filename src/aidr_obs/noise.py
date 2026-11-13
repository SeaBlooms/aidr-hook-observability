"""Known-harmless debug-log errors.

The shipped list is ``config/known_noise.json``. A file of the same shape in the data
directory replaces it, which is what the dashboard writes when the list is edited.
A rule matches when ``pattern`` is contained in the folded error signature and, if set,
``contains`` occurs in the raw message. The signature folds paths away, so a filename
has to be matched on the raw message.
"""

import json
import os
from pathlib import Path

from . import paths
from .logparse import error_signature

_DEFAULT = Path(__file__).with_name("config") / "known_noise.json"


def rules_path() -> Path:
    return paths.data_dir() / "known_noise.json"


def load_rules() -> list[dict]:
    path = rules_path() if rules_path().is_file() else _DEFAULT
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [rule for rule in data if isinstance(rule, dict) and rule.get("id")]


def using_defaults() -> bool:
    return not rules_path().is_file()


def match_rule(message: str, rules: list[dict] | None = None) -> dict | None:
    rules = load_rules() if rules is None else rules
    signature = error_signature(message or "")
    for rule in rules:
        contains = rule.get("contains") or ""
        pattern = rule.get("pattern") or ""
        if contains and contains not in (message or ""):
            continue
        if pattern and pattern not in signature:
            continue
        if contains or pattern:
            return rule
    return None


def save_rules(rules: list) -> list[dict]:
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    if len(rules) > 100:
        raise ValueError("too many rules")
    clean = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("each rule must be an object")
        rid = str(rule.get("id") or "").strip()
        if not rid or len(rid) > 80 or rid in seen:
            raise ValueError("each rule needs its own id")
        seen.add(rid)
        clean.append(
            {
                "id": rid,
                "pattern": str(rule.get("pattern") or "")[:300],
                "contains": str(rule.get("contains") or "")[:200],
                "explanation": str(rule.get("explanation") or "")[:400],
            }
        )
    path = rules_path()
    paths.ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, indent=2)
        handle.write("\n")
    return clean

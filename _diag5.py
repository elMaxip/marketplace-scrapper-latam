"""Throwaway: which word is doing the rejecting, and does it hit the title or
only the description?"""

import sys
import types
from collections import Counter

sys.path.insert(0, "src")
stub = types.ModuleType("magic")
stub.from_file = lambda *a, **k: ""
stub.from_buffer = lambda *a, **k: ""
stub.Magic = type("Magic", (), {"__init__": lambda self, *a, **k: None})
sys.modules.setdefault("magic", stub)

from ai_marketplace_monitor.config import Config  # noqa: E402
from ai_marketplace_monitor.observations import iter_observations  # noqa: E402
from ai_marketplace_monitor.utils import amm_home, cache, fold_text  # noqa: E402

config = Config([amm_home / "config.toml"])
item = next(iter(config.item.values()))
words = [fold_text(w) for w in (item.antikeywords or [])]
print("item:", item.name)
print("antikeywords:", item.antikeywords)
print("keywords:", item.keywords)

title_only = Counter()
description_only = Counter()
rejected = 0
for record in iter_observations():
    if record.get("deleted") or record.get("matched", True):
        continue
    rejected += 1
    snapshot = record.get("listing") or {}
    title = fold_text(str(snapshot.get("title") or ""))
    description = fold_text(str(snapshot.get("description") or ""))
    for word in words:
        if word in title:
            title_only[word] += 1
        elif word in description:
            description_only[word] += 1

print(f"\nrejected listings: {rejected}")
print("\nword found in the TITLE (probably a correct rejection):")
for word, count in title_only.most_common():
    print(f"   {word:<12} {count}")
print("\nword found ONLY in the DESCRIPTION (a console that mentions it):")
for word, count in description_only.most_common():
    print(f"   {word:<12} {count}")

print("\nexamples rejected on the description alone:")
shown = 0
for record in iter_observations():
    if record.get("deleted") or record.get("matched", True) or shown >= 6:
        continue
    snapshot = record.get("listing") or {}
    title = fold_text(str(snapshot.get("title") or ""))
    description = fold_text(str(snapshot.get("description") or ""))
    hit = next((w for w in words if w in description and w not in title), None)
    if hit:
        shown += 1
        where = description.index(hit)
        print(f"   {str(snapshot.get('title'))[:55]:<55} <- {hit!r} en: "
              f"...{description[max(0, where - 40):where + 40]}...")

cache.close()

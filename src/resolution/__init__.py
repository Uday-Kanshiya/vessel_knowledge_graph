"""
Entity resolution (02 §3.9). Stage 1, the deterministic alias table, lives here:

  normalize.py      rule-based cleaning: casefold, punctuation, abbreviation expansion, position split;
                    the shared config tables (config/modifier_terms.csv, config/vessel_names.txt)
  lookup.py         SQLite store (standard_term, alias, family, pending_term, resolution_log):
                    resolve / find / review / merge / queue / promote / dismiss, CSV in/out
  diff.py           compare two store files: the re-resolve loop is rebuild + diff (docs/13 §6)
  seed_aliases.csv  the human-editable starting vocabulary

Later stages (embedding nearest-neighbour, LLM adjudicator) are not here yet (03 §1); the mint
guard hook in `LookupStore.resolve` is where their refusals land (pending_term).
Part 3 code only: nothing under src/extraction imports this package (03 §2).
"""

from src.resolution.lookup import LookupStore, PendingRow, Resolution, StandardRow
from src.resolution.normalize import Cleaned, ResolverConfig, load_config, normalize

__all__ = [
    "Cleaned",
    "LookupStore",
    "PendingRow",
    "Resolution",
    "ResolverConfig",
    "StandardRow",
    "load_config",
    "normalize",
]

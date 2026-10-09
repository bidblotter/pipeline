#!/usr/bin/env python3
"""Contractor name normalization for PermitPicker.

Two-level approach:
  contractor_key(name)   -> deterministic grouping key (same company -> same key)
  clean_name(name)       -> cleaned single name, original casing (dba resolved,
                           license numbers and corp suffixes stripped)
  is_contractor(name)    -> False for placeholder values (tenant, owner, ...)

Callers group by contractor_key, then pick the most common clean_name per key
as the display name (majority vote preserves acronyms like HVAC).
Cross-key aliases that fuzzy matching can't safely resolve live in ALIASES.
"""
import re

CORP_SUFFIX = r'\b(llc|inc|co|corp|ltd|pllc|pa|lp|llp)\b\.?'

NON_CONTRACTOR = {
    'tenant', 'owner', 'owner (general)', 'homeowner', 'n/a', 'none',
    'tbd', 'unknown', 'same', 'self',
}

# Hand-verified cross-key merges (key -> canonical key). Add cautiously:
# false merges are worse than duplicates.
ALIASES = {
    'custom air conditioning dba': 'custom air conditioning',
    'a maynor heating &': 'a maynor heating & air',
    'green horiz': 'green horizon',
}


def _dba_resolve(name):
    """'MOF dba: dba Michael & Son Services of NC' -> 'Michael & Son Services of NC'."""
    if re.search(r'\bdba\b', name, re.I):
        parts = [p.strip(' :()') for p in re.split(r'\bdba\b', name, flags=re.I)]
        # Prefer the last substantial part (the DBA trade name);
        # fall back to the front when the tail is empty or just punctuation.
        # DBA at the end ('CUSTOM AIR CONDITIONING DBA') -> keep the front part.
        cands = [c for c in reversed(parts) if re.search(r'[A-Za-z]{2,}', c)]
        name = cands[0] if cands else parts[0]
    return name.strip(' :')


def contractor_key(name):
    n = (name or '').strip()
    if not n:
        return ''
    n = _dba_resolve(n).lower()
    n = re.sub(r'[.,]', '', n)
    n = re.sub(r'\(\s*\d+\s*\)', '', n)   # (31142)
    n = re.sub(r'\b\d{4,6}\b', '', n)     # trailing license numbers
    n = re.sub(CORP_SUFFIX, '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return ALIASES.get(n, n)


def clean_name(name):
    n = (name or '').strip()
    if not n:
        return ''
    n = _dba_resolve(n)
    n = re.sub(r'\(\s*\d+\s*\)', '', n)
    n = re.sub(r'\b\d{4,6}\b', '', n)
    n = re.sub(r'\s+', ' ', n).strip(' ,.')
    n = re.sub(r'[\s,]+' + CORP_SUFFIX + r'$', '', n, flags=re.I).strip(' ,.')
    return n or (name or '').strip()


def is_contractor(name):
    n = (name or '').strip().lower().rstrip('.')
    return bool(n) and n not in NON_CONTRACTOR


def display_for(key, names):
    """Pick display name: most common clean_name among raw variants."""
    from collections import Counter
    c = Counter(clean_name(n) for n in names if clean_name(n))
    return c.most_common(1)[0][0] if c else ''

"""Centralized checklist normalization utilities.

This consolidates the logic previously duplicated across:
- models.ObservationDetail.get_checklist
- PDF/email Jinja templates macros
- extended_form template key checking

Key goals:
1. Provide a single normalize_label(label: str) -> normalized_key
2. Provide variant generation for lookups: generate_variants(group_key, label)
3. Provide a function normalize_mapping(group_key, raw_mapping) that returns a dict with both prefixed and bare variants
4. Provide a function value_for(group_key, mapping, label) -> bool for Jinja usage

All normalization rules:
- Lowercase
- Replace " - " with space
- Replace dashes with spaces
- Remove apostrophes
- Collapse whitespace to single underscores
- Remove leading/trailing underscores
- For org_mgmt: also recognize 'organisation_and_class_management_' legacy prefix and collapse to 'org_mgmt_'
- Provide both prefixed and unprefixed variants for each Boolean that is True
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Set

__all__ = [
    'normalize_label','generate_variants','normalize_mapping','value_for'
]

_space_re = re.compile(r"[\s]+")
_dupe_us_re = re.compile(r"__+")


def _base_normalize(text: str) -> str:
    t = (text or '').strip().lower()
    t = t.replace(' - ', ' ')
    t = t.replace('-', ' ')
    t = t.replace("'", '')
    t = _space_re.sub('_', t)
    t = _dupe_us_re.sub('_', t)
    return t.strip('_')


def normalize_label(label: str) -> str:
    return _base_normalize(label)


def generate_variants(group_key: str | None, label: str) -> Set[str]:
    base = normalize_label(label)
    variants: Set[str] = {base}
    if group_key:
        if not base.startswith(group_key + '_'):
            variants.add(f"{group_key}_{base}")
        else:
            # If double-prefixed (group_group_rest), collapse one
            double = f"{group_key}_{group_key}_"
            if base.startswith(double):
                collapsed = base[len(group_key)+1:]
                variants.add(collapsed)
            # Add bare suffix
            suffix = base[len(group_key)+1:]
            if suffix:
                variants.add(suffix)
    # Organisation legacy prefix handling
    if group_key == 'org_mgmt' and base.startswith('organisation_and_class_management_'):
        tail = base[len('organisation_and_class_management_'):]
        variants.add('org_mgmt_' + tail)
        variants.add(tail)
    # De-dupe underscores variant
    punct_free = base.replace('__','_')
    variants.add(punct_free)
    return {v for v in variants if v}


def normalize_mapping(group_key: str, raw_mapping: Dict[str, bool]) -> Dict[str, bool]:
    """Return a mapping containing both prefixed and bare variants for truthy keys.

    raw_mapping may contain keys in any of these forms:
      - prefixed: weekly_test_marked_on_time
      - double-prefixed: weekly_test_weekly_test_marked_on_time
      - bare: marked_on_time (after UI stripping)
    We produce a canonical set of True values while preserving False only if no True for that logical flag.
    """
    if not isinstance(raw_mapping, dict):
        return {}
    fixed: Dict[str, bool] = {}
    prefix_once = group_key + '_'
    double_prefix = prefix_once + prefix_once
    for raw_k, raw_v in raw_mapping.items():
        if not isinstance(raw_k, str):
            continue
        k = raw_k.strip()
        if k.startswith(double_prefix):
            k = k[len(prefix_once):]
        k_norm = normalize_label(k)
        v_bool = bool(raw_v)
        # Merge truthy
        if k_norm in fixed:
            fixed[k_norm] = fixed[k_norm] or v_bool
        else:
            fixed[k_norm] = v_bool
    # Build variants dict
    result: Dict[str, bool] = {}
    for k, v in fixed.items():
        variants = generate_variants(group_key, k)
        for vk in variants:
            if v:
                result[vk] = True
            else:
                result.setdefault(vk, False)
    return result


def value_for(group_key: str, mapping: Dict[str,bool], label: str) -> bool:
    if not mapping:
        return False
    for variant in generate_variants(group_key, label):
        if mapping.get(variant):
            return True
    return False

# Jinja integration helper (call from app factory): provide a filter and a global.

def register_checklist_jinja(env):
    env.globals['checklist_value_for'] = value_for
    env.filters['checklist_value_for'] = lambda mapping, group_key, label: value_for(group_key, mapping, label)
    return env

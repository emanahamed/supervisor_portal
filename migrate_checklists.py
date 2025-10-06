"""One-off migration script to canonicalize checklist JSON keys.

Usage:
  python migrate_checklists.py

This will:
  - Load each ObservationDetail
  - Parse each checklist JSON field
  - Use checklist_utils.normalize_mapping to produce canonical variants
  - Store only the canonical prefixed keys (group_key + '_' + bare) for True values
  - False values are omitted (sparse True map)
  - Commit changes

Safety:
  - Creates a backup JSON dump (backup_checklists_<timestamp>.json) of original data before modifying.
"""
from __future__ import annotations

import json
import time

from app import app, db
from checklist_utils import normalize_mapping
from models import ObservationDetail

CANON_FIELDS = ['weekly_test','homework','classwork','org_mgmt']

def canonical_true_only(group_key: str, mapping: dict) -> dict:
    norm = normalize_mapping(group_key, mapping)
    result = {}
    prefix = group_key + '_'
    for k, v in norm.items():
        if not v:
            continue
        # ensure prefixed form only
        if not k.startswith(prefix):
            k = prefix + k
        result[k] = True
    return result

with app.app_context():
    details = ObservationDetail.query.all()
    backup = []
    for d in details:
        entry = {'id': d.id}
        changed = False
        for field in CANON_FIELDS:
            raw_text = getattr(d, field)
            try:
                raw_map = json.loads(raw_text) if raw_text else {}
            except Exception:
                raw_map = {}
            entry[field] = raw_map
            canon = canonical_true_only(field, raw_map)
            if canon != raw_map:
                setattr(d, field, json.dumps(canon))
                changed = True
        if changed:
            backup.append(entry)
    if backup:
        fname = f"backup_checklists_{int(time.time())}.json"
        with open(fname,'w') as f:
            json.dump(backup, f, indent=2)
        db.session.commit()
        print(f"Updated {len(backup)} observation detail records. Backup saved to {fname}.")
    else:
        print("No changes required; all records already canonical.")

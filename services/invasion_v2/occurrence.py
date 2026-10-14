"""Deterministic v2 occurrence identity, separate from legacy invasion keys."""

import re
from collections.abc import Collection


def build_occurrence_key(group_id: int, region_code: str, slot_epoch: int) -> str:
    """Build an unambiguous key for one group, region and UTC slot epoch."""
    if isinstance(group_id, bool) or not isinstance(group_id, int):
        raise ValueError("group_id must be an integer")
    if not isinstance(region_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", region_code):
        raise ValueError("region_code must be a nonempty region code (max 64 characters)")
    if isinstance(slot_epoch, bool) or not isinstance(slot_epoch, int) or slot_epoch < 0:
        raise ValueError("slot_epoch must be a nonnegative integer")
    return f"inv2:v1:group:{group_id}:region:{region_code}:slot:{slot_epoch}"


def is_duplicate_occurrence(
    existing_keys: Collection[str], *, group_id: int, region_code: str, slot_epoch: int
) -> bool:
    """Pure idempotency check against a caller-supplied collection of keys."""
    return build_occurrence_key(group_id, region_code, slot_epoch) in existing_keys

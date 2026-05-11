from typing import Any, Dict, Optional

_VALID_VENDORS = frozenset({"HP", "DELL", "CISCO"})


def resolve_server_vendor(spec: Dict[str, Any], annotations: Optional[Dict[str, str]]) -> Optional[str]:
    """
    Resolve server vendor with spec.server_vendor taking priority over annotations.

    Priority: spec.server_vendor > metadata.annotations.server_vendor

    Returns normalized uppercase string (HP, DELL, or CISCO), or None if not specified.
    Spec values are already validated by the CRD OpenAPI schema (pattern: (?i)^(HP|DELL|CISCO)$),
    so only annotation values need runtime validation.
    Raises ValueError with a clear message if an annotation value is not HP, DELL, or CISCO.
    """
    spec_value = spec.get('server_vendor')
    if spec_value:
        # CRD schema already enforced the value — just normalize case
        return spec_value.strip().upper()

    annotation_value = annotations.get('server_vendor') if annotations else None
    if annotation_value:
        normalized = annotation_value.strip().upper()
        if normalized not in _VALID_VENDORS:
            raise ValueError(
                f"Invalid server_vendor {annotation_value!r} in annotation — "
                f"must be one of: HP, DELL, CISCO (case-insensitive). "
                f"Got: {annotation_value!r}"
            )
        return normalized

    return None


def resolve_vlan_id(spec: Dict[str, Any], annotations: Optional[Dict[str, str]]) -> Optional[str]:
    """
    Resolve VLAN ID with spec.network.vlanId taking priority over annotations.

    Priority: spec.network.vlanId > metadata.annotations.vlanId

    Returns VLAN ID as string, or None if not specified in either place.
    Spec values are already validated by the CRD OpenAPI schema (type: integer, minimum: 1,
    maximum: 4094), so only annotation values need runtime validation.
    Raises ValueError with a clear message if an annotation value is not a valid integer in [1, 4094].
    """
    vlan_from_spec = (spec.get('network') or {}).get('vlanId')
    if vlan_from_spec is not None:
        # CRD schema already enforced the type and range — just convert
        return str(int(vlan_from_spec))

    vlan_from_annotation = annotations.get('vlanId') if annotations else None
    if not vlan_from_annotation:
        return None

    vlan_str = str(vlan_from_annotation).strip()
    if not vlan_str:
        return None

    try:
        vlan_int = int(vlan_str)
    except ValueError:
        raise ValueError(
            f"Invalid vlanId in annotation — "
            f"must be an integer between 1 and 4094. "
            f"Got: {vlan_from_annotation!r}"
        )

    if not (1 <= vlan_int <= 4094):
        raise ValueError(
            f"Invalid vlanId in annotation — "
            f"must be between 1 and 4094. "
            f"Got: {vlan_int}"
        )

    return str(vlan_int)
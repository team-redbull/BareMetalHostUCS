"""
Server profile configuration for BareMetalHost Generator Operator.

Loads server type → NIC/MAC-index profiles from a YAML file mounted from a
Kubernetes ConfigMap. Falls back to built-in defaults if the file is absent,
so the operator keeps working without any ConfigMap deployed.

Override SERVER_PROFILES_PATH env var for local development.

Each profile maps a server-name pattern to one OR two network interfaces. Two
interfaces are used to build a bonded NMStateConfig (see yaml_generators.py).

Expected file format (profiles.yaml):
  profiles:
    - pattern: "h100"               # matched case-insensitively against server name
      nic_names: [ens8f0np0, ens8f1np1]   # bond members, in order
      mac_indices: ["0", "1"]             # how to pick each member's MAC (parallel to nic_names)
    - pattern: "10tb-"
      nic_names: [ens2f0np0]              # single interface → non-bonded config
      mac_indices: ["last"]
    - default: true                        # fallback entry — no pattern
      nic_names: [eno12399np0]
      mac_indices: ["first"]

nic_names and mac_indices are required, parallel lists of equal length.

A mac_index value is "first", "last", or a 0-based integer string. Its meaning
per vendor:
  - HP / Cisco UCS / Intersight: position in the ordered list of NIC MACs.
  - Dell OME: index into the list of network interfaces (kept for back-compat;
    "first"/"last" also walk first/last port + partition).

To add a new server type, append an entry to the ConfigMap and apply it. No
image rebuild required.
"""

import os
from typing import List, Optional, Tuple

import yaml

from src.config import bmh_logger

logger = bmh_logger

_DEFAULT_PROFILES_PATH = "/config/profiles.yaml"

# Built-in defaults — used when the ConfigMap file is absent (local dev, missing
# mount, etc.). Two-NIC bond pairs; configure real NIC names in the ConfigMap.
_BUILTIN_PROFILES = [
    {"pattern": "h100",  "nic_names": ["ens8f0np0", "ens8f1np1"],   "mac_indices": ["2", "3"]},
    {"pattern": "h200",  "nic_names": ["ens33f0np0", "ens33f1np1"], "mac_indices": ["2", "3"]},
    {"pattern": "10tb-", "nic_names": ["ens2f0np0", "ens2f1np1"],   "mac_indices": ["last", "first"]},
    {"default": True,    "nic_names": ["eno12399np0", "eno12409np1"], "mac_indices": ["first", "last"]},
]

# Module-level singleton — populated on first call to get_server_profile_config()
_profiles: Optional[list] = None


def _load_profiles() -> list:
    """Load profiles from file or return built-in defaults."""
    path = os.getenv("SERVER_PROFILES_PATH", _DEFAULT_PROFILES_PATH)
    if not os.path.exists(path):
        logger.warning(
            f"Server profiles file not found at '{path}'; using built-in defaults"
        )
        return _BUILTIN_PROFILES
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
        profiles = data.get("profiles", [])
        if not profiles:
            raise ValueError("profiles list is empty")
        logger.info(f"Loaded {len(profiles)} server profiles from '{path}'")
        return profiles
    except Exception as exc:
        logger.error(
            f"Failed to load server profiles from '{path}': {exc}; "
            "using built-in defaults"
        )
        return _BUILTIN_PROFILES


def get_server_profile_config() -> list:
    """
    Return the loaded list of server profiles (singleton).

    Profiles are loaded once at first call. Restart the operator pod to pick
    up ConfigMap changes.
    """
    global _profiles
    if _profiles is None:
        _profiles = _load_profiles()
    return _profiles


def _normalize_profile(profile: dict) -> dict:
    """
    Return a copy of profile with ``nic_names`` and ``mac_indices`` as parallel
    lists of equal length.

    Raises:
        ValueError: if either key is missing/empty or the lists differ in length.
    """
    nic_names = list(profile.get("nic_names") or [])
    # Coerce mac_indices entries to strings so downstream comparisons are uniform.
    mac_indices = [str(i) for i in (profile.get("mac_indices") or [])]

    if not nic_names or not mac_indices:
        raise ValueError(
            f"Profile {profile.get('pattern', 'default')!r} must define non-empty "
            f"nic_names and mac_indices lists"
        )
    if len(nic_names) != len(mac_indices):
        raise ValueError(
            f"Profile {profile.get('pattern', 'default')!r} has "
            f"{len(nic_names)} nic_names but {len(mac_indices)} mac_indices; "
            "they must be parallel lists of equal length"
        )

    return {**profile, "nic_names": nic_names, "mac_indices": mac_indices}


def lookup_profile(server_name: str) -> dict:
    """
    Return the first profile whose pattern appears in server_name (case-insensitive),
    normalized to carry parallel ``nic_names`` / ``mac_indices`` lists.

    Falls back to the entry marked default=true when no pattern matches.

    Raises:
        RuntimeError: If no default entry exists and no pattern matches.
    """
    server_lower = server_name.lower()
    default_entry = None

    for profile in get_server_profile_config():
        if profile.get("default"):
            default_entry = profile
            continue
        pattern = profile.get("pattern", "")
        if pattern.lower() in server_lower:
            normalized = _normalize_profile(profile)
            logger.info(
                f"Profile match '{pattern}' for '{server_name}': "
                f"nic_names={normalized['nic_names']}, mac_indices={normalized['mac_indices']}"
            )
            return normalized

    if default_entry is None:
        raise RuntimeError(
            f"No matching profile and no default entry for server '{server_name}'"
        )

    normalized = _normalize_profile(default_entry)
    logger.info(
        f"No pattern matched for '{server_name}'; using default profile: "
        f"nic_names={normalized['nic_names']}, mac_indices={normalized['mac_indices']}"
    )
    return normalized


def select_from_ordered(candidates: list, index) -> object:
    """
    Select one element from an ordered list using an index spec.

    Args:
        candidates: ordered list to pick from (e.g. NIC MAC addresses).
        index: "first" (or None/""), "last", or a 0-based integer / integer string.

    Returns:
        The selected element.

    Raises:
        IndexError: if candidates is empty or the resolved position is out of range.
        ValueError: if index is neither a keyword nor a valid integer.
    """
    if not candidates:
        raise IndexError("cannot select from an empty candidate list")
    if index in (None, "", "first"):
        return candidates[0]
    if index == "last":
        return candidates[-1]
    return candidates[int(index)]  # raises ValueError (bad str) / IndexError (out of range)


def resolve_nics_and_mac_indices(
    server_name: str,
    nic_names_override: Optional[List[str]] = None,
    mac_indices_override: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """
    Return ``(nic_names, mac_indices)`` as parallel lists.

    Uses spec overrides when provided; otherwise falls back to lookup_profile().
    Callers store the result in CR status for user visibility and pass
    mac_indices down to the vendor strategy to select the right NIC MACs.
    """
    if nic_names_override:
        return list(nic_names_override), [str(i) for i in (mac_indices_override or [])]
    profile = lookup_profile(server_name)
    return list(profile["nic_names"]), list(profile["mac_indices"])


def select_macs(
    ordered_macs: List[str],
    mac_indices: Optional[List[str]],
    server_name: str,
) -> List[str]:
    """
    Select NIC MAC addresses from an ordered candidate list using mac_indices.

    Shared by the flat-list vendor strategies (HP, Cisco UCS, Intersight). Dell
    keeps its own hierarchical selection. When mac_indices is None/empty, falls
    back to the server's profile so a strategy can be used standalone.

    Args:
        ordered_macs: ordered NIC MACs as returned by the management system.
        mac_indices: list of "first"/"last"/integer specs (parallel to nic_names).
        server_name: used for the profile fallback and logging.

    Returns:
        The selected MACs in order, or [] if any index cannot be resolved.
    """
    if not mac_indices:
        _, mac_indices = resolve_nics_and_mac_indices(server_name)
    selected: List[str] = []
    for idx in mac_indices:
        try:
            selected.append(select_from_ordered(ordered_macs, idx))
        except (IndexError, ValueError) as exc:
            logger.error(
                f"Cannot select MAC index {idx!r} for '{server_name}' from "
                f"{len(ordered_macs)} available NIC MAC(s): {exc}"
            )
            return []
    return selected

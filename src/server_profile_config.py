"""
Server profile configuration for BareMetalHost Generator Operator.

Loads server type → NIC/MAC-index profiles from a YAML file mounted from a
Kubernetes ConfigMap. Falls back to built-in defaults if the file is absent,
so the operator keeps working without any ConfigMap deployed.

Override SERVER_PROFILES_PATH env var for local development.

Expected file format (profiles.yaml):
  profiles:
    - pattern: "h100"          # matched case-insensitively against server name
      nic_name: "ens8f0np0"
      mac_index: "2"           # "first", "last", or 0-based integer string
    - pattern: "h200"
      nic_name: "ens33f0np0"
      mac_index: "2"
    - pattern: "10tb-"
      nic_name: "ens2f0np0"
      mac_index: "last"
    - default: true            # fallback entry — no pattern
      nic_name: "eno12399np0"
      mac_index: "first"

To add a new server type (e.g. dgxh100, b200), append an entry to the ConfigMap
and apply it. No image rebuild required.
"""

import os
from typing import Optional

import yaml

from src.config import bmh_logger

logger = bmh_logger

_DEFAULT_PROFILES_PATH = "/config/profiles.yaml"

# Built-in defaults — identical to the previously hardcoded behaviour.
# Used when the ConfigMap file is absent (local dev, missing mount, etc.)
_BUILTIN_PROFILES = [
    {"pattern": "h100",   "nic_name": "ens8f0np0",   "mac_index": "2"},
    {"pattern": "h200",   "nic_name": "ens33f0np0",  "mac_index": "2"},
    {"pattern": "10tb-",  "nic_name": "ens2f0np0",   "mac_index": "last"},
    {"default": True,     "nic_name": "eno12399np0", "mac_index": "first"},
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


def lookup_profile(server_name: str) -> dict:
    """
    Return the first profile whose pattern appears in server_name (case-insensitive).
    Falls back to the entry marked default=true when no pattern matches.

    Args:
        server_name: The server name to match against.

    Returns:
        Profile dict with at least 'nic_name' and 'mac_index' keys.

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
            logger.info(
                f"Profile match '{pattern}' for '{server_name}': "
                f"nic_name={profile['nic_name']}, mac_index={profile['mac_index']}"
            )
            return profile

    if default_entry is None:
        raise RuntimeError(
            f"No matching profile and no default entry for server '{server_name}'"
        )

    logger.info(
        f"No pattern matched for '{server_name}'; "
        f"using default profile: nic_name={default_entry['nic_name']}, "
        f"mac_index={default_entry['mac_index']}"
    )
    return default_entry


def resolve_nic_and_mac_index(server_name: str,
                               nic_name_override: Optional[str] = None,
                               mac_index_override: Optional[str] = None):
    """
    Return (effective_nic_name, effective_mac_index).

    Uses spec overrides when provided; otherwise falls back to lookup_profile().
    Callers can store the result in CR status for user visibility.
    """
    if nic_name_override:
        return nic_name_override, mac_index_override
    profile = lookup_profile(server_name)
    return profile['nic_name'], profile['mac_index']

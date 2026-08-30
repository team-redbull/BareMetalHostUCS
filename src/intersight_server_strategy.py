"""
Cisco Intersight server strategy.

Two data sources, because Intersight does NOT map per-server NIC MAC addresses:

1. **Cisco Intersight Python SDK** (`intersight` on PyPI) — looks the server up by
   name and returns its **CIMC management IP**. The SDK handles the HTTP Signature
   (HS2019 / RSA-SHA256) auth from an API key id + private key.
   Docs: https://github.com/CiscoDevNet/intersight-python

2. **The CIMC's Redfish API** (queried directly with `requests` using the BMC
   credentials) — provides the NIC **MAC addresses**. We walk
   ``/redfish/v1/Chassis/1/NetworkAdapters/{adapter}/NetworkPorts/{port}``, keep
   only ports whose ``LinkStatus`` is ``Up``, and return their MACs in a stable
   order. ``select_macs()`` then picks the requested bond members, exactly like
   the other vendors.

Required configuration (read in unified_server_client.initialize_unified_client):
  - INTERSIGHT_API_ENDPOINT  e.g. https://intersight.com (SaaS) or appliance URL
  - INTERSIGHT_API_KEY_ID    the API key id from Intersight
  - INTERSIGHT_API_SECRET    the PEM private key — either the key text itself or a
                             path to the .pem file
  - INTERSIGHT_BMC_USERNAME / INTERSIGHT_BMC_PASSWORD  CIMC creds, used BOTH for
                             the BMH secret AND to query the CIMC Redfish API here.
                             Passed in via the unified client's credentials dict
                             (bmc_username/bmc_password); falls back to these env
                             vars when the strategy is built standalone.

The `intersight` package is imported lazily inside ensure_connected() so this
module (and the operator) still import cleanly when Intersight is not used.
"""

import os
from typing import Dict, List, Optional, Tuple

import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

from src.server_strategy import ServerStrategy
from src.config import intersight_strategy_logger
from src.server_profile_config import select_macs

disable_warnings(InsecureRequestWarning)
logger = intersight_strategy_logger

# CIMC chassis that holds the NetworkAdapters (per the documented Redfish path).
_REDFISH_CHASSIS = "1"
_REDFISH_TIMEOUT = 30


class IntersightServerStrategy(ServerStrategy):

    def __init__(self, credentials: Dict[str, str]):
        self.credentials = credentials
        self.endpoint = (credentials.get("endpoint") or "https://intersight.com").rstrip("/")
        self.api_key_id = credentials.get("api_key_id")
        self._api_secret = credentials.get("api_secret")
        # CIMC creds for the Redfish MAC query — from the unified client's
        # credentials dict, falling back to env so the strategy also works when
        # instantiated standalone.
        self._bmc_username = credentials.get("bmc_username") or os.getenv("INTERSIGHT_BMC_USERNAME")
        self._bmc_password = credentials.get("bmc_password") or os.getenv("INTERSIGHT_BMC_PASSWORD")
        self._api_client = None  # intersight.ApiClient, built lazily
        self._cache = None

    # ------------------------------------------------------------------ config
    def is_configured(self) -> bool:
        """Check that Intersight management creds AND CIMC BMC creds are present."""
        management_configured = all([
            self.endpoint,
            self.api_key_id,
            self._api_secret,
        ])

        bmc_configured = all([
            self._bmc_username,
            self._bmc_password,
        ])

        if management_configured and not bmc_configured:
            logger.warning(
                "Cisco Intersight is configured but INTERSIGHT_BMC_USERNAME/"
                "INTERSIGHT_BMC_PASSWORD are missing (also needed to read NIC MACs "
                "from the CIMC Redfish API)"
            )

        return management_configured and bmc_configured

    # -------------------------------------------------------------- connection
    def ensure_connected(self) -> None:
        if self._api_client is not None:
            return

        try:
            import intersight
            from intersight import signing
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "Cisco Intersight support requires the 'intersight' package. "
                "Install it with: pip install intersight"
            ) from exc

        # The private key may be the PEM text itself or a path to a .pem file.
        signing_kwargs = {}
        secret = self._api_secret
        if secret and os.path.exists(secret):
            signing_kwargs["private_key_path"] = secret
        else:
            signing_kwargs["private_key_string"] = secret

        signing_info = signing.HttpSigningConfiguration(
            key_id=self.api_key_id,
            signing_scheme=signing.SCHEME_HS2019,
            signing_algorithm=signing.ALGORITHM_RSASSA_PKCS1v15,
            hash_algorithm=signing.HASH_SHA256,
            signed_headers=[
                signing.HEADER_REQUEST_TARGET,
                signing.HEADER_HOST,
                signing.HEADER_DATE,
                signing.HEADER_DIGEST,
            ],
            **signing_kwargs,
        )

        configuration = intersight.Configuration(
            host=self.endpoint,
            signing_info=signing_info,
        )
        # Match the operator's convention for the other vendor clients and
        # support on-prem appliances with self-signed certificates.
        configuration.verify_ssl = False

        self._api_client = intersight.ApiClient(configuration)
        logger.info(f"Initialized Intersight API client for {self.endpoint}")

    # ---------------------------------------------------------------- queries
    def get_server_info(
        self, server_name: str, mac_indices: Optional[List[str]] = None
    ) -> Tuple[List[str], Optional[str]]:
        self.ensure_connected()
        logger.info(f"Searching Intersight for server: {server_name}")

        from intersight.api import compute_api

        # 1) Intersight: find the physical server by name → CIMC management IP.
        compute = compute_api.ComputeApi(self._api_client)
        summary = compute.get_compute_physical_summary_list(
            filter=f"Name eq '{server_name}'"
        )
        results = getattr(summary, "results", None) or []
        if not results:
            logger.error(f"Server {server_name} not found in Intersight")
            return [], None

        server = results[0]
        mgmt_ip = getattr(server, "mgmt_ip_address", None) or getattr(server, "management_ip", None)
        if not mgmt_ip:
            logger.error(f"No management (CIMC) IP for Intersight server {server_name}")
            return [], None
        logger.info(f"Intersight {server_name}: CIMC mgmt IP={mgmt_ip}")

        # 2) CIMC Redfish: collect link-up NIC MACs (Intersight doesn't map them).
        ordered_macs = self._get_macs_from_cimc_redfish(mgmt_ip)
        if not ordered_macs:
            logger.error(
                f"No link-up NIC MACs found via CIMC Redfish for {server_name}"
            )
            return [], None

        macs = select_macs(ordered_macs, mac_indices, server_name)
        if not macs:
            logger.error(
                f"Failed to select requested NIC MAC(s) for {server_name} "
                f"from {len(ordered_macs)} link-up port(s)"
            )
            return [], None

        logger.info(f"Intersight {server_name}: selected MACs={macs}, mgmt IP={mgmt_ip}")
        return macs, mgmt_ip

    # ------------------------------------------------------------ CIMC Redfish
    def _get_macs_from_cimc_redfish(self, cimc_ip: str) -> List[str]:
        """Return MACs of every link-up NIC port on the CIMC, in stable order.

        Walks ``/redfish/v1/Chassis/{chassis}/NetworkAdapters/*/NetworkPorts/*``
        and keeps only ports whose ``LinkStatus`` is ``Up``. Port paths are sorted
        so the resulting order is deterministic for first/last/index selection.
        """
        username = self._bmc_username
        password = self._bmc_password
        if not username or not password:
            logger.error(
                "INTERSIGHT_BMC_USERNAME/INTERSIGHT_BMC_PASSWORD are required to "
                "query the CIMC Redfish API for NIC MACs"
            )
            return []

        base = f"https://{cimc_ip}"
        session = requests.Session()
        session.auth = (username, password)
        session.verify = False
        try:
            # Gather every NetworkPort path across all NetworkAdapters.
            port_paths: List[str] = []
            adapters = self._redfish_get(
                session, f"{base}/redfish/v1/Chassis/{_REDFISH_CHASSIS}/NetworkAdapters"
            )
            for adapter_ref in adapters.get("Members", []):
                adapter_path = adapter_ref.get("@odata.id")
                if not adapter_path:
                    continue
                ports = self._redfish_get(session, f"{base}{adapter_path}/NetworkPorts")
                for member in ports.get("Members", []):
                    if member.get("@odata.id"):
                        port_paths.append(member["@odata.id"])

            # Stable order so "first"/"last"/index selection is deterministic.
            port_paths.sort()

            macs: List[str] = []
            for port_path in port_paths:
                port = self._redfish_get(session, f"{base}{port_path}")
                status = port.get("LinkStatus")
                if str(status).lower() not in ("up", "linkup"):
                    logger.debug(f"Skipping CIMC port {port_path}: LinkStatus={status!r}")
                    continue
                for mac in self._extract_port_macs(port):
                    macs.append(mac)
                    logger.debug(f"Link-up CIMC port {port_path} -> {mac}")
            return macs
        except Exception as exc:
            logger.error(f"Error querying CIMC Redfish at {base}: {exc}")
            return []
        finally:
            session.close()

    @staticmethod
    def _redfish_get(session: "requests.Session", url: str) -> dict:
        resp = session.get(url, timeout=_REDFISH_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _extract_port_macs(port: dict) -> List[str]:
        """Pull MAC(s) from a Redfish NetworkPort object.

        Primary field is the NetworkPort schema's ``AssociatedNetworkAddresses``;
        fall back to the newer Port schema's ``Ethernet.AssociatedMACAddresses``.
        """
        addrs = port.get("AssociatedNetworkAddresses")
        if not addrs:
            addrs = (port.get("Ethernet") or {}).get("AssociatedMACAddresses")
        return [a for a in (addrs or []) if a]

    # ----------------------------------------------------------------- cleanup
    def clear_cache(self):
        self._cache = None

    def disconnect(self) -> None:
        if self._api_client is not None:
            try:
                # ApiClient holds a urllib3 pool manager; close it if available.
                close = getattr(self._api_client, "close", None)
                if callable(close):
                    close()
            except Exception as e:  # pragma: no cover
                logger.warning(f"Error closing Intersight API client: {e}")
            finally:
                self._api_client = None

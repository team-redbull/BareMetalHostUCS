import logging
import os
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Tuple, Dict, Type, List
import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from src.server_strategy import ServerStrategy
from src.config import cisco_strategy_logger
from src.server_profile_config import select_macs

disable_warnings(InsecureRequestWarning)
logger = cisco_strategy_logger

class CiscoServerStrategy(ServerStrategy):
    
    def __init__(self, credentials: Dict[str, str]):
        self.credentials = credentials
        self._ucsc_handle = None
        self._UcsHandle = None
        self._cache = None
    
    def is_configured(self) -> bool:
        """Check if Cisco UCS Central and BMC credentials are configured."""
        # Check management system credentials
        management_configured = all([
            self.credentials.get("central_ip"),
            self.credentials.get("central_username"),
            self.credentials.get("central_password"),
            self.credentials.get("manager_username"),
            self.credentials.get("manager_password")
        ])

        # Check BMC credentials
        bmc_configured = all([
            os.getenv('CISCO_BMC_USERNAME'),
            os.getenv('CISCO_BMC_PASSWORD')
        ])

        if management_configured and not bmc_configured:
            logger.warning("Cisco UCS is configured but CISCO_BMC_USERNAME/CISCO_BMC_PASSWORD are missing")

        return management_configured and bmc_configured
    
    def ensure_connected(self) -> None:
        if not self._ucsc_handle:
            central_ip = self.credentials.get('central_ip')
            logger.info(f"Connecting to UCS Central at: {central_ip}")

            if not central_ip:
                raise ValueError("UCS_CENTRAL_IP is not configured")

            try:
                from ucscsdk.ucschandle import UcscHandle
                from ucsmsdk.ucshandle import UcsHandle

                logger.debug(f"Creating UcscHandle with IP={central_ip}, username={self.credentials.get('central_username')}")

                self._ucsc_handle = UcscHandle(
                    central_ip,
                    self.credentials['central_username'],
                    self.credentials['central_password']
                )

                logger.info(f"Attempting login to UCS Central at {central_ip}...")
                self._ucsc_handle.login()
                self._UcsHandle = UcsHandle

                logger.info(f"Successfully connected to UCS Central at {central_ip}")
            except ImportError as e:
                logger.error("UCS SDK is not installed. Please install ucscsdk and ucsmsdk packages.")
                raise
            except Exception as e:
                logger.error(f"Failed to connect to UCS Central at {central_ip}: {type(e).__name__}: {e}")
                logger.exception("Full UCS connection error details:")
                raise
        
    def get_server_info(
        self, server_name: str, mac_indices: Optional[List[str]] = None
    ) -> Tuple[List[str], Optional[str]]:
        self.ensure_connected()

        if self._cache is None:
            self._cache = self._ucsc_handle.query_classid("lsServer")
        
        for server in self._cache:
            if server.name.upper() == server_name.upper():
                domain = server.domain
                logger.info(f"Found server {server_name} in UCS Central, domain: '{domain}'")
                logger.debug(f"Server DN: {server.dn}")

                # Log server attributes for debugging
                try:
                    logger.debug(f"Server attributes: name={server.name}, dn={server.dn}, domain='{domain}', "
                               f"pn_dn={getattr(server, 'pn_dn', 'N/A')}, type={getattr(server, 'type', 'N/A')}")
                except Exception as e:
                    logger.debug(f"Could not log all server attributes: {e}")

                # Check if domain is empty or None
                if not domain or domain.strip() == "":
                    logger.error(f"Server {server_name} has EMPTY domain value in UCS Central")
                    logger.error(f"Server DN: {server.dn}")
                    logger.error(f"This server is not assigned to a UCS Manager domain or the domain value is not set")
                    logger.error(f"Please check UCS Central configuration and ensure the server is assigned to a domain")
                    return [], None

                ucsm_handle = None

                try:
                    logger.info(f"Connecting to UCS Manager at domain: '{domain}'")
                    ucsm_handle = self._UcsHandle(
                        domain,
                        self.credentials['manager_username'],
                        self.credentials['manager_password']
                    )

                    logger.debug(f"Attempting login to UCS Manager at {domain}...")
                    ucsm_handle.login()
                    logger.info(f"Successfully connected to UCS Manager at {domain}")

                    server_details = self._ucsc_handle.query_dn(server.dn)
                    if not server_details:
                        logger.warning(f"Could not query server details for DN: {server.dn}")
                        continue

                    kvm_ip = self._extract_ucs_management_ip(ucsm_handle, server_details)
                    logger.debug(f"Extracted KVM IP: {kvm_ip}")

                    ordered_macs = self._extract_ucs_mac_addresses(ucsm_handle, server_details)
                    logger.debug(f"Extracted ordered MAC addresses: {ordered_macs}")
                    macs = select_macs(ordered_macs, mac_indices, server_name)

                    if macs and kvm_ip:
                        logger.info(f"Successfully retrieved server info for {server_name}: MACs={macs}, IP={kvm_ip}")
                        return macs, kvm_ip
                    else:
                        logger.warning(f"Incomplete server info for {server_name}: MACs={macs}, IP={kvm_ip}")

                except Exception as e:
                    logger.error(f"Error connecting to UCS Manager at {domain}: {type(e).__name__}: {e}")
                    logger.exception("Full UCS Manager connection error:")
                    # Continue to next server if multiple matches (shouldn't happen but be safe)

                finally:
                    if ucsm_handle:
                        try:
                            ucsm_handle.logout()
                            logger.debug(f"Logged out from UCS Manager at {domain}")
                        except Exception as e:
                            logger.warning(f"Error during UCS Manager logout: {e}")
        return [], None
        
    def _extract_ucs_management_ip(self, ucsm_handle, server_details) -> Optional[str]:
        try:
            mgmt_interfaces = ucsm_handle.query_children(
                in_mo=server_details,
                class_id="VnicIpV4PooledAddr"
            )

            for iface in mgmt_interfaces:
                if hasattr(iface, "addr") and iface.addr:
                    return str(iface.addr)
        except Exception as e:
            logger.warning(f"Failed to extract UCS management IP: {e}")

        return None

    def _extract_ucs_mac_addresses(self, ucsm_handle, server_details) -> List[str]:
        """Return all vNIC MACs in sorted adapter order (eth0, eth1, ...).

        select_macs() then picks the requested members (e.g. first + second) per
        the server profile's mac_indices, so the bonded pair is configurable.
        """
        try:
            adapters = ucsm_handle.query_children(
                in_mo=server_details,
                class_id="VnicEther"
            )

            if adapters:
                # Sort by adapter name (strip first 3 chars if name is long enough, e.g., "eth0" -> "0")
                # Handle short names gracefully
                sorted_adapters = sorted(adapters, key=lambda x: x.name[3:] if len(x.name) > 3 else x.name)
                return [a.addr for a in sorted_adapters if hasattr(a, "addr") and a.addr]
        except Exception as e:
            logger.warning(f"Failed to extract UCS MAC addresses: {e}")

        return []
    
    def clear_cache(self):
        """Clear any cached data."""
        self._cache = None
    
    def disconnect(self): 
        if self._ucsc_handle:
            try: 
                self._ucsc_handle.logout()
                logger.info("Successfully disconnected from UCS central")
            except Exception as e:
                logger.warning(f"Error during UCS central logout: {e}")
            finally:
                self._ucsc_handle = None
                
            
            
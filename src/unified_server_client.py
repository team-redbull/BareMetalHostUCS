import logging
from enum import Enum
import os
from typing import List, Optional, Tuple, Dict

import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from src.server_strategy import ServerType, ServerTypeDetector, ServerStrategy, ServerStrategyFactory 
# Disable SSL warnings
disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)

class UnifiedServerClient:
    """
    Unified client for managing HP, Cisco UCS, and Dell servers.
    Automatically detects server type based on naming convention and searches efficiently.
    """
    
    def __init__(self, 
                 # HP OneView credentials
                 oneview_ip=None, 
                 oneview_username=None, 
                 oneview_password=None,
                 # UCS Central/Manager credentials
                 ucs_central_ip=None, 
                 central_username=None, 
                 central_password=None,
                 manager_username=None, 
                 manager_password=None,
                 # Dell OME credentials
                 ome_ip=None, 
                 ome_username=None, 
                 ome_password=None):
        """
        Initialize unified server client with credentials for all systems.
        
        Args:
            oneview_ip: HP OneView IP address
            oneview_username: HP OneView username
            oneview_password: HP OneView password
            ucs_central_ip: Cisco UCS Central IP address
            central_username: UCS Central username
            central_password: UCS Central password
            manager_username: UCS Manager username
            manager_password: UCS Manager password
            ome_ip: Dell OpenManage Enterprise IP address
            ome_username: Dell OME username
            ome_password: Dell OME password
        """
        self._credentials = {
            ServerType.HP: {
                "ip": oneview_ip,
                "username": oneview_username,
                "password": oneview_password
            },
            ServerType.CISCO: {
                "central_ip": ucs_central_ip,
                "central_username": central_username,
                "central_password": central_password,
                "manager_username": manager_username,
                "manager_password": manager_password
            },
            ServerType.DELL: {
                "ip": ome_ip,
                "username": ome_username,
                "password": ome_password
            }
         }
        self._strategies: Dict[ServerType, ServerStrategy] = {}
        self._initialize_strategies()
        self._detector = ServerTypeDetector()
        logger.info("Initialized UnifiedServerClient")
    
    def _initialize_strategies(self):
        for server_type, credentials in self._credentials.items():
            try:
                strategy = ServerStrategyFactory.create_strategy(server_type, credentials)
                if strategy.is_configured():
                    self._strategies[server_type] = strategy
                    logger.info(f"Initialized strategy for {server_type.value}")
            except Exception as e:
                logger.error(f"Error initializing strategy for {server_type.value}: {str(e)}")
    
    def _get_search_order(self, detected_type: ServerType, server_vendor: Optional[str] = None) -> List[ServerType]:
        if server_vendor:
            vendor_type = self._detector.detect("", server_vendor)
            return [vendor_type, ServerType.DELL, ServerType.CISCO, ServerType.HP]
        
        search_priority = {
            ServerType.HP: [ServerType.HP, ServerType.CISCO, ServerType.DELL],
            ServerType.DELL: [ServerType.DELL, ServerType.CISCO, ServerType.HP],
            ServerType.CISCO: [ServerType.CISCO, ServerType.DELL, ServerType.HP]
        }
        return search_priority.get(detected_type, [ServerType.DELL, ServerType.CISCO, ServerType.HP])
    def get_server_info(self, server_name: str, server_vendor: Optional[str] = None,
                        mac_index_override: Optional[str] = None) -> Tuple[str, str]:
        logger.info(f"Retrieving server info for: {server_name}")
        detected_type = self._detector.detect(server_name, server_vendor)
        search_order = self._get_search_order(detected_type, server_vendor)
        try:
            for server_type in search_order:
                strategy = self._strategies.get(server_type)
                if not strategy:
                    logger.debug(f"No configured strategy for {server_type.value} ")
                    continue
                try:
                    logger.info(f"Searching in {server_type.value} system")
                    # mac_index_override only applies to Dell; HP/Cisco strategies
                    # have their own fixed MAC selection logic and ignore it.
                    if server_type == ServerType.DELL:
                        mac, ip = strategy.get_server_info(server_name, mac_index_override)
                    else:
                        mac, ip = strategy.get_server_info(server_name)
                    if mac and ip:
                        logger.info(f"Found server in {server_type.value} system with {ip},{mac}")
                        self.disconnect()
                        return mac, ip
                except Exception as e:
                    logger.warning(f"Error searching in {server_type.value} system: {str(e)}")
                    continue
            self.disconnect()
            raise ValueError(f"Server {server_name} not found in any configured system")
        except Exception as e:
            self.disconnect()
            raise e
        
        
    def disconnect(self):
        """Disconnect from all systems"""
        for server_type, strategy in self._strategies.items():
            try:
                strategy.disconnect()
                strategy.clear_cache()
            except Exception as e:
                logger.warning(f"Error disconnecting from {server_type.value}: {str(e)}")
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure cleanup"""
        self.disconnect()
        
def initialize_unified_client():
    """Initialize the unified client by setting up strategies for all configured systems."""
    oneview_ip = os.getenv("ONEVIEW_IP")
    oneview_username = os.getenv("ONEVIEW_USERNAME")
    oneview_password = os.getenv("ONEVIEW_PASSWORD")
    
    ucs_central_ip = os.getenv("UCS_CENTRAL_IP")
    central_username = os.getenv("UCS_CENTRAL_USERNAME")
    central_password = os.getenv("UCS_CENTRAL_PASSWORD")
    manager_username = os.getenv("UCS_MANAGER_USERNAME")
    manager_password = os.getenv("UCS_MANAGER_PASSWORD")
    
    ome_ip = os.getenv("OME_IP")
    ome_username = os.getenv("OME_USERNAME")
    ome_password = os.getenv("OME_PASSWORD")
    
    hp_configured = all([oneview_ip, oneview_username, oneview_password])
    cisco_configured = any([ucs_central_ip and central_username and central_password,
                            manager_username and manager_password])
    dell_configured = all([ome_ip, ome_username, ome_password])
    
    if not any([hp_configured, cisco_configured, dell_configured]):
        raise ValueError("No valid configuration found for HP OneView, Cisco UCS, or Dell OME.")
    
    configured_systems = []
    if hp_configured:
        configured_systems.append("HP OneView")
    if cisco_configured:
        configured_systems.append("Cisco UCS")
    if dell_configured:
        configured_systems.append("Dell OME")
    
    logger.info(f"Configured systems: {', '.join(configured_systems)}")
    
    unified_client = UnifiedServerClient(
        oneview_ip=oneview_ip,
        oneview_username=oneview_username,
        oneview_password=oneview_password,
        ucs_central_ip=ucs_central_ip,
        central_username=central_username,
        central_password=central_password,
        manager_username=manager_username,
        manager_password=manager_password,
        ome_ip=ome_ip,
        ome_username=ome_username,
        ome_password=ome_password
    )
    return unified_client
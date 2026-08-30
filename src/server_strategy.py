import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Tuple, Dict, Type, List
import requests
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning
from src.config import operator_logger

disable_warnings(InsecureRequestWarning)
logger = operator_logger

class ServerType(Enum):
    HP = "hp"
    DELL = "dell"
    CISCO = "cisco"
    INTERSIGHT = "intersight"
    UNKNOWN = "unknown"

class ServerStrategy(ABC):
    
    def __init__(self, credentials: Dict[str, str]):
        self.credentials = credentials
        self._cache = None
        self._session = None 
        self._auth_token = None
        
    @abstractmethod
    def is_configured(self) -> bool:
        """Check if the server type is properly configured."""
        pass
    
    @abstractmethod
    def ensure_connected(self) -> None:
        """Ensure that a connection to management system."""
        pass
    
    @abstractmethod
    def get_server_info(
        self, server_name: str, mac_indices: Optional[List[str]] = None
    ) -> Tuple[List[str], Optional[str]]:
        """Retrieve (ordered list of NIC MAC addresses, BMC/management IP).

        Args:
            server_name: server to look up in the management system.
            mac_indices: which NIC MACs to return ("first"/"last"/integer specs).
                One entry per bond member. When None/empty, the strategy falls
                back to the server's profile.

        Returns:
            (macs, ip) where macs is ordered and parallel to the resolved
            nic_names. Returns ([], None) when the server is not found.
        """
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the management system."""
        pass
    
    def clear_cache(self):
        """Clear any cached data."""
        self._cache = None
    
class ServerTypeDetector:
    # Ordered list of (substring, ServerType) checked against the lowercase server name.
    # First match wins. Extend this list to support new naming conventions.
    _NAME_PATTERNS = [
        ("hp",         ServerType.HP),
        ("dell",       ServerType.DELL),
        ("intersight", ServerType.INTERSIGHT),
        ("cisco",      ServerType.CISCO),
    ]
    _DEFAULT_TYPE = ServerType.CISCO

    @classmethod
    def detect(cls, server_name: str, server_vendor: Optional[str] = None) -> ServerType:
        if server_vendor:
            vendor_upper = server_vendor.strip().upper()
            logger.debug(f"Server vendor provided: {vendor_upper}")
            for _, server_type in cls._NAME_PATTERNS:
                if server_type.name == vendor_upper:
                    logger.debug(f"Detected server type from vendor: {server_type.name}")
                    return server_type
            logger.info(f"Server vendor {vendor_upper!r} not recognized, falling back to name-based detection.")

        server_name_lower = server_name.lower()
        for keyword, server_type in cls._NAME_PATTERNS:
            if keyword in server_name_lower:
                logger.debug(f"Detected server type {server_type.name!r} from server name (matched {keyword!r}).")
                return server_type

        logger.debug(f"No name pattern matched, defaulting to {cls._DEFAULT_TYPE.name}.")
        return cls._DEFAULT_TYPE

class ServerStrategyFactory:
    # Lazy import to avoid circular dependency
    _strategies: Dict[ServerType, Type[ServerStrategy]] = {}

    @classmethod
    def _init_strategies(cls):
        """Lazy initialization of strategies to avoid circular imports"""
        if not cls._strategies:
            from src.hp_server_strategy import HPServerStrategy
            from src.dell_server_strategy import DellServerStrategy
            from src.ucs_server_strategy import CiscoServerStrategy
            from src.intersight_server_strategy import IntersightServerStrategy

            cls._strategies = {
                ServerType.HP: HPServerStrategy,
                ServerType.DELL: DellServerStrategy,
                ServerType.CISCO: CiscoServerStrategy,
                ServerType.INTERSIGHT: IntersightServerStrategy,
            }

    @classmethod
    def create_strategy(cls, server_type: ServerType, credentials: Dict[str, str]) -> ServerStrategy:
        cls._init_strategies()  # Ensure strategies are loaded
        strategy_class = cls._strategies.get(server_type)
        if not strategy_class:
            raise ValueError(f"No strategy found for server type: {server_type}")
        return strategy_class(credentials)
    
    @classmethod
    def register_strategy(cls, server_type: ServerType, strategy_class: Type[ServerStrategy]) -> None:
        cls._strategies[server_type] = strategy_class
        
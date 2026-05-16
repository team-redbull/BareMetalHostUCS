"""
Central configuration module for BareMetalHost Generator Operator.

This module provides:
- Centralized logging configuration
- Buffer management constants
- Kubernetes CRD constants (BMHGen, BMH, NMStateConfig)
- Status phase constants

All vendor-specific logic (BMC credentials, address formats) is in yaml_generators.py.
Management system credentials are read directly in unified_server_client.py.
"""

import logging
import os


# ============================================================================
# Logging Configuration
# ============================================================================

def get_log_level() -> int:
    """Get log level from environment variable or default to INFO."""
    log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    log_levels = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    return log_levels.get(log_level_str, logging.INFO)


def setup_logging():
    """Configure logging for the entire application."""
    logging.basicConfig(
        level=get_log_level(),
        format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    )


# Initialize logging on module import
setup_logging()

# Create specialized loggers and set their levels
bmh_logger = logging.getLogger('bmh_generator')
bmh_logger.setLevel(get_log_level())

ucs_logger = logging.getLogger('ucs_client')
ucs_logger.setLevel(get_log_level())

operator_logger = logging.getLogger('k8s_operator')
operator_logger.setLevel(get_log_level())

buffer_logger = logging.getLogger('bmh_buffer')
buffer_logger.setLevel(get_log_level())

# Strategy loggers
hp_strategy_logger = logging.getLogger('hp_strategy')
hp_strategy_logger.setLevel(get_log_level())

dell_strategy_logger = logging.getLogger('dell_strategy')
dell_strategy_logger.setLevel(get_log_level())

cisco_strategy_logger = logging.getLogger('cisco_strategy')
cisco_strategy_logger.setLevel(get_log_level())


# ============================================================================
# Buffer Management Configuration
# ============================================================================

# Maximum number of available (non-provisioned) BareMetalHosts allowed
MAX_AVAILABLE_SERVERS = int(os.getenv('MAX_AVAILABLE_SERVERS', '20'))

# Interval in seconds between buffer checks
BUFFER_CHECK_INTERVAL = int(os.getenv('BUFFER_CHECK_INTERVAL', '30'))


# ============================================================================
# Resource Deletion Configuration
# ============================================================================

def str_to_bool(value: str) -> bool:
    """Convert string environment variable to boolean."""
    return value.lower() in ('true', '1', 'yes', 'on')

# Control whether to delete all resources (BMH, Secret, NMStateConfig) when BareMetalHostGenerator is deleted
# Set to 'false' to preserve resources after BareMetalHostGenerator deletion
DELETE_RESOURCES_ON_DELETE = str_to_bool(os.getenv('DELETE_RESOURCES_ON_DELETE', 'true'))


# ============================================================================
# Server Profile Configuration
# ============================================================================
# Path to the YAML file (mounted from a ConfigMap) that defines server type
# → NIC name and MAC-index profiles. See src/server_profile_config.py.
SERVER_PROFILES_PATH = os.getenv('SERVER_PROFILES_PATH', '/config/profiles.yaml')


# ============================================================================
# Kubernetes CRD Constants
# ============================================================================

# BareMetalHostGenerator CRD Constants
class BMHGenCRD:
    """Constants for BareMetalHostGenerator Custom Resource Definition"""
    GROUP = "infra.example.com"
    VERSION = "v1alpha1"
    PLURAL = "baremetalhostgenerators"
    KIND = "BareMetalHostGenerator"
    FINALIZER = "bmhgenerator.infra.example.com/finalizer"


# BareMetalHost CRD Constants (Metal3)
class BMHCRD:
    """Constants for BareMetalHost Custom Resource Definition"""
    GROUP = "metal3.io"
    VERSION = "v1alpha1"
    PLURAL = "baremetalhosts"
    KIND = "BareMetalHost"


# NMStateConfig CRD Constants
class NMStateConfigCRD:
    """Constants for NMStateConfig Custom Resource Definition"""
    GROUP = "agent-install.openshift.io"
    VERSION = "v1beta1"
    PLURAL = "nmstateconfigs"
    KIND = "NMStateConfig"


# ============================================================================
# Status Phase Constants
# ============================================================================

class Phase:
    """Status phases for BareMetalHostGenerator"""
    PROCESSING = "Processing"
    BUFFERED = "Buffered"
    COMPLETED = "Completed"
    FAILED = "Failed"


# ============================================================================
# MongoDB Integration (opt-in)
# ============================================================================

# Set MONGO_URI to enable MongoDB-first server lookup.
# Leave empty to use vendor APIs directly (original behaviour).
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "server_scanner")



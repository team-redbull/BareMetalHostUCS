import base64
import ipaddress
import os
import re
from typing import Any, Dict, Optional, Tuple
from src.server_strategy import ServerType
import yaml
from src.config import bmh_logger, BMHCRD, NMStateConfigCRD
from src.server_profile_config import lookup_profile


# ============================================================================
# BMC Credential Helper Functions
# ============================================================================

def get_bmc_credentials(vendor: str) -> Tuple[str, str]:
    """
    Get BMC credentials for a specific vendor from environment variables.

    Args:
        vendor: Vendor name (HP, DELL, or CISCO)

    Returns:
        Tuple of (username, password) in plain text

    Raises:
        ValueError: If credentials are not configured for the vendor
    """
    vendor_upper = vendor.upper()

    if vendor_upper == "HP":
        username = os.getenv('HP_BMC_USERNAME')
        password = os.getenv('HP_BMC_PASSWORD')
        vendor_name = "HP"
    elif vendor_upper == "DELL":
        username = os.getenv('DELL_BMC_USERNAME')
        password = os.getenv('DELL_BMC_PASSWORD')
        vendor_name = "DELL"
    elif vendor_upper == "CISCO":
        username = os.getenv('CISCO_BMC_USERNAME')
        password = os.getenv('CISCO_BMC_PASSWORD')
        vendor_name = "CISCO"
    else:
        raise ValueError(f"Unknown vendor: {vendor}. Must be HP, DELL, or CISCO")

    if not username or not password:
        raise ValueError(
            f"Missing BMC credentials for {vendor_name}. "
            f"Please set {vendor_name}_BMC_USERNAME and {vendor_name}_BMC_PASSWORD environment variables"
        )

    return username, password


def get_bmc_address(vendor: str, ip_address: str) -> str:
    """
    Get vendor-specific BMC address format.

    These formats are static and do not change per deployment.

    Args:
        vendor: Vendor name (HP, DELL, or CISCO)
        ip_address: Management IP address

    Returns:
        Formatted BMC address string
    """
    vendor_upper = vendor.upper()

    if vendor_upper == "HP":
        return f"redfish-virtualmedia://{ip_address}/redfish/v1/Systems/1"
    elif vendor_upper == "DELL":
        return f"idrac-virtualmedia://{ip_address}/redfish/v1/Systems/System.Embedded.1"
    elif vendor_upper == "CISCO":
        return f"ipmi://{ip_address}:623"
    else:
        # Default to IPMI if vendor is unknown
        return f"ipmi://{ip_address}"


def get_secret_name(vendor: str, server_name: str) -> str:
    """
    Get vendor-specific secret name format.

    Format: {vendor}-cred-{server_name}

    Args:
        vendor: Vendor name (HP, DELL, or CISCO)
        server_name: Name of the server

    Returns:
        Formatted secret name (e.g., "hp-cred-server01")
    """
    vendor_lower = vendor.lower()

    if vendor_lower in ["hp", "dell", "cisco"]:
        return f"{vendor_lower}-cred-{server_name}"
    else:
        return f"bmc-cred-{server_name}"


class YamlGenerator:
    def __init__(self):
        self.bmh_logger = bmh_logger

    def _get_interface_name(self, server_name: str, nic_name_override: Optional[str] = None) -> str:
        """
        Determine the network interface name based on server type.

        If nic_name_override is provided it is used directly, bypassing profile lookup.

        Args:
            server_name: Name of the server
            nic_name_override: Optional explicit NIC name from spec.networkConfig.nicName

        Returns:
            Interface name string
        """
        if nic_name_override:
            self.bmh_logger.info(
                f"Server '{server_name}' → nic_name='{nic_name_override}' (spec override)"
            )
            return nic_name_override
        profile = lookup_profile(server_name)
        self.bmh_logger.info(f"Server '{server_name}' → nic_name='{profile['nic_name']}'")
        return profile["nic_name"]

    def validate_inputs(self, mac: str, ip: str) -> None:
        """Validate MAC and IP address formats"""
        self.bmh_logger.debug(f"Validating inputs - MAC: {mac}, IP: {ip}")
        
        if not mac or not ip:
            self.bmh_logger.error("MAC address and/or IP address is empty")
            raise ValueError("MAC address and IP address must not be empty")

        MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")

        if not MAC_RE.fullmatch(mac):
            self.bmh_logger.error(f"Invalid MAC address format: {mac}")
            raise ValueError(f"Invalid MAC address format: {mac}")

        try:
            ipaddress.IPv4Address(ip)
            self.bmh_logger.debug(f"Successfully validated IP address: {ip}")
        except ipaddress.AddressValueError as exc:
            self.bmh_logger.error(f"Invalid IPv4 address: {ip} - {exc}")
            raise ValueError(f"Invalid IPv4 address: {ip}") from exc

        self.bmh_logger.info(f"Input validation successful for MAC: {mac}, IP: {ip}")


    def validate_yaml_format(self, data: Dict[str, Any]) -> None:
        """Validate that the generated data can be properly serialized to YAML format"""
        self.bmh_logger.debug("Validating YAML format of generated data")

        try:
            yaml.dump(data, default_flow_style=False)
            self.bmh_logger.debug("YAML validation successful")
        except yaml.YAMLError as exc:
            self.bmh_logger.error(f"YAML validation failed: {exc}")
            raise ValueError(f"Generated data cannot be converted to valid YAML: {exc}") from exc


    def generate_baremetal_host(
        self,
        name: str,
        namespace: str,
        server_vendor: str,
        mac_address: str,
        ipmi_address: str,
        infra_env: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Generate BareMetalHost resource definition.

        Note: BMC credentials are stored in secrets and retrieved from config.py
        based on server_vendor. No need to pass username/password here.
        """
        self.bmh_logger.info(f"Generating BareMetalHost for {name} in namespace {namespace}")
        self.bmh_logger.debug(f"Parameters - MAC: {mac_address}, IPMI: {ipmi_address}, InfraEnv: {infra_env}")
        
        self.validate_inputs(mac_address, ipmi_address)

        # Get vendor-specific BMC address and secret name
        bmc_address = get_bmc_address(server_vendor, ipmi_address)
        secret_name = get_secret_name(server_vendor, name)
        
        bmh_data = {
            "apiVersion": f"{BMHCRD.GROUP}/{BMHCRD.VERSION}",
            "kind": BMHCRD.KIND,
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "infraenvs.agent-install.openshift.io": infra_env,
                    **(labels or {}),
                },
                "annotations": {
                    "inspect.metal3.io": "disabled",
                    "bmac.agent-install.openshift.io/hostname": name,
                },
            },
            "spec": {
                "online": True,
                "bootMACAddress": mac_address,
                "hardwareProfile": "empty",
                "customDeploy": {
                    "method": "start_assisted_install"
                },
                "automatedCleaningMode": "disabled",
                "bmc": {
                    "address": f"{bmc_address}",
                    "credentialsName": f"{secret_name}",
                    "disableCertificateVerification": True,
                },
                "bootMode": "UEFI",
            },
        }
        
        if labels:
            self.bmh_logger.debug(f"Additional labels applied: {labels}")
        
        self.validate_yaml_format(bmh_data)
        self.bmh_logger.info(f"Successfully generated BareMetalHost definition for {name}")
        
        return bmh_data


    def generate_bmc_secret(
        self,
        name: str,
        namespace: str,
        server_vendor: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate BMC Secret resource definition.

        Credentials are retrieved from environment variables:
        - HP_BMC_USERNAME / HP_BMC_PASSWORD for HP servers
        - DELL_BMC_USERNAME / DELL_BMC_PASSWORD for Dell servers
        - CISCO_BMC_USERNAME / CISCO_BMC_PASSWORD for Cisco servers

        Input credentials from environment are in plain text.
        Output secret data is base64 encoded for Kubernetes.

        Args:
            name: Server name
            namespace: Kubernetes namespace
            server_vendor: Vendor name (HP, DELL, CISCO)
            username: Optional BMC username override (uses env var if not provided)
            password: Optional BMC password override (uses env var if not provided)

        Returns:
            Secret resource dictionary with base64-encoded credentials

        Raises:
            ValueError: If BMC credentials are not configured for the vendor
        """
        self.bmh_logger.info(f"Generating BMC secret for {name} in namespace {namespace}")

        # Get vendor-specific credentials from environment if not provided
        if username is None or password is None:
            env_username, env_password = get_bmc_credentials(server_vendor)
            username = username or env_username
            password = password or env_password
            self.bmh_logger.debug(f"Using BMC credentials from environment for {server_vendor}")

        # Get vendor-specific secret name
        secret_name = get_secret_name(server_vendor, name)

        # Encode credentials to base64 for Kubernetes secret
        secret_data = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "type": "Opaque",
            "data": {
                "username": base64.b64encode(username.encode()).decode(),
                "password": base64.b64encode(password.encode()).decode(),
            },
        }

        self.validate_yaml_format(secret_data)
        self.bmh_logger.info(f"Successfully generated BMC secret definition for {secret_name}")
        return secret_data
    
    def generate_nmstate_config(self, name: str, namespace: str, macAddress: str, infra_env: str, vlanId: str,
                                nic_name_override: Optional[str] = None) -> Dict[str, Any]:
        """Generate NMStateConfig resource definition"""
        self.bmh_logger.info(f"Generating NMStateConfig for {name} in namespace {namespace}")
        self.bmh_logger.info(f"MacAddress: {macAddress}")

        # Validate VLAN ID
        if not vlanId or vlanId == "None" or str(vlanId).strip() == "":
            raise ValueError(f"VLAN ID is required for NMStateConfig but got: {vlanId}")

        # Validate VLAN ID is numeric and convert to integer
        try:
            vlan_id_int = int(vlanId)
            if vlan_id_int < 1 or vlan_id_int > 4094:
                raise ValueError(f"VLAN ID must be between 1 and 4094, got: {vlanId}")
        except ValueError as e:
            raise ValueError(f"VLAN ID must be a valid integer, got: {vlanId}") from e

        # Determine interface name based on server type (or spec override)
        interface_name = self._get_interface_name(name, nic_name_override)
        self.bmh_logger.info(f"Configuring the nmstateconfig with {interface_name}.{vlan_id_int}")
        nmstate_data = {
            "apiVersion": f"{NMStateConfigCRD.GROUP}/{NMStateConfigCRD.VERSION}",
            "kind": NMStateConfigCRD.KIND,
            "metadata": {
                "labels": {
                    "infraenvs.agent-install.openshift.io": infra_env
                },
                "name": f"nmstate-config-{name}",
                "namespace": namespace
            },
            "spec": {
                "config": {
                    "interfaces": [
                        {
                            "ipv4": {
                                "enabled": False
                            },
                            "ipv6": {
                                "enabled": False
                            },
                            "mac-address": macAddress,
                            "name": f"{interface_name}",
                            "state": "up",
                            "type": "ethernet"
                        },
                        {
                            "ipv4":{
                                "auto-dns": True,
                                "auto-gateway": True,
                                "auto-routes": True,
                                "dhcp": True,
                                "enabled": True
                            },
                            "ipv6": {
                                "autoconf": False,
                                "dhcp": False,
                                "enabled": False
                            },
                            "name": f"{interface_name}.{vlan_id_int}",
                            "state": "up",
                            "type": "vlan",
                            "vlan": {
                                "base-iface": f"{interface_name}",
                                "id": vlan_id_int
                            }
                        }
                    ]
                }, 
                "interfaces": [
                    {
                        "macAddress": macAddress,
                        "name": f"{interface_name}"
                    }
                ]
            }
        }
        self.validate_yaml_format(nmstate_data)
        self.bmh_logger.info(f"Successfully generated NMStateConfig definition for {name}")
        return nmstate_data
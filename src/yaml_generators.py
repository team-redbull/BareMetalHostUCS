import base64
import ipaddress
import os
import re
from typing import Any, Dict, Optional, Tuple
from src.server_strategy import ServerType
import yaml
from src.config import bmh_logger, BMHCRD, NMStateConfigCRD


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
    elif vendor_upper == "INTERSIGHT":
        username = os.getenv('INTERSIGHT_BMC_USERNAME')
        password = os.getenv('INTERSIGHT_BMC_PASSWORD')
        vendor_name = "INTERSIGHT"
    else:
        raise ValueError(f"Unknown vendor: {vendor}. Must be HP, DELL, CISCO, or INTERSIGHT")

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
    elif vendor_upper == "INTERSIGHT":
        # Cisco Intersight-managed UCS servers expose Redfish on the CIMC.
        # NOTE: confirm the exact Systems path against your CIMC firmware.
        return f"redfish-virtualmedia://{ip_address}/redfish/v1/Systems/1"
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

    if vendor_lower in ["hp", "dell", "cisco", "intersight"]:
        return f"{vendor_lower}-cred-{server_name}"
    else:
        return f"bmc-cred-{server_name}"


class YamlGenerator:
    def __init__(self):
        self.bmh_logger = bmh_logger

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
    
    @staticmethod
    def _validate_vlan_id(vlanId: str) -> int:
        """Validate and return the VLAN id as an int (1-4094)."""
        if not vlanId or vlanId == "None" or str(vlanId).strip() == "":
            raise ValueError(f"VLAN ID is required for NMStateConfig but got: {vlanId}")
        try:
            vlan_id_int = int(vlanId)
        except (TypeError, ValueError) as e:
            raise ValueError(f"VLAN ID must be a valid integer, got: {vlanId}") from e
        if vlan_id_int < 1 or vlan_id_int > 4094:
            raise ValueError(f"VLAN ID must be between 1 and 4094, got: {vlanId}")
        return vlan_id_int

    @staticmethod
    def _ethernet_iface(nic_name: str, mac: str) -> Dict[str, Any]:
        """A physical ethernet member interface with IP disabled."""
        return {
            "ipv4": {"enabled": False},
            "ipv6": {"enabled": False},
            "mac-address": mac,
            "name": nic_name,
            "state": "up",
            "type": "ethernet",
        }

    @staticmethod
    def _vlan_iface(base_iface: str, vlan_id_int: int) -> Dict[str, Any]:
        """The DHCP VLAN interface riding on top of base_iface (a NIC or a bond)."""
        return {
            "ipv4": {
                "auto-dns": True,
                "auto-gateway": True,
                "auto-routes": True,
                "dhcp": True,
                "enabled": True,
            },
            "ipv6": {"autoconf": False, "dhcp": False, "enabled": False},
            "name": f"{base_iface}.{vlan_id_int}",
            "state": "up",
            "type": "vlan",
            "vlan": {"base-iface": base_iface, "id": vlan_id_int},
        }

    @staticmethod
    def _bond_options(bond_mode: str) -> Dict[str, str]:
        options = {"miimon": "100"}
        if bond_mode == "802.3ad":
            options["lacp_rate"] = "fast"
        return options

    def generate_nmstate_config(self, name: str, namespace: str, mac_addresses, nic_names,
                                infra_env: str, vlanId: str, bond_name: str = "bond0",
                                bond_mode: str = "802.3ad") -> Dict[str, Any]:
        """Generate an NMStateConfig resource.

        With two (or more) NICs it builds a bond (default 802.3ad/LACP) of the
        members with the VLAN riding on the bond. With a single NIC it falls back
        to the original non-bonded shape (VLAN directly on the NIC).

        Args:
            mac_addresses: NIC MAC addresses (parallel to nic_names).
            nic_names: OS interface names for each bond member (parallel to macs).
            bond_name: name of the bond interface (when bonding).
            bond_mode: link-aggregation mode (e.g. "802.3ad", "active-backup").
        """
        self.bmh_logger.info(f"Generating NMStateConfig for {name} in namespace {namespace}")
        self.bmh_logger.info(f"MACs: {mac_addresses}, NICs: {nic_names}")

        vlan_id_int = self._validate_vlan_id(vlanId)

        if not mac_addresses or not nic_names:
            raise ValueError(
                f"NMStateConfig for {name} requires at least one NIC and MAC; "
                f"got nic_names={nic_names}, mac_addresses={mac_addresses}"
            )
        if len(mac_addresses) != len(nic_names):
            raise ValueError(
                f"NMStateConfig for {name}: nic_names ({len(nic_names)}) and "
                f"mac_addresses ({len(mac_addresses)}) must be the same length"
            )

        # Reuse the existing MAC format validation (ip is irrelevant here).
        MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
        for mac in mac_addresses:
            if not mac or not MAC_RE.fullmatch(mac):
                raise ValueError(f"Invalid MAC address format for {name}: {mac!r}")

        if len(nic_names) == 1:
            # Single interface — preserve the original non-bonded layout.
            iface, mac = nic_names[0], mac_addresses[0]
            self.bmh_logger.info(f"Configuring single-interface nmstateconfig with {iface}.{vlan_id_int}")
            config_interfaces = [
                self._ethernet_iface(iface, mac),
                self._vlan_iface(iface, vlan_id_int),
            ]
            spec_interfaces = [{"macAddress": mac, "name": iface}]
        else:
            # Two (or more) interfaces — bond them and put the VLAN on the bond.
            self.bmh_logger.info(
                f"Configuring bonded nmstateconfig {bond_name} (mode={bond_mode}) "
                f"over {nic_names} with VLAN {vlan_id_int}"
            )
            members = [self._ethernet_iface(n, m) for n, m in zip(nic_names, mac_addresses)]
            bond_iface = {
                "ipv4": {"enabled": False},
                "ipv6": {"enabled": False},
                "name": bond_name,
                "state": "up",
                "type": "bond",
                "link-aggregation": {
                    "mode": bond_mode,
                    "options": self._bond_options(bond_mode),
                    "port": list(nic_names),
                },
            }
            config_interfaces = members + [bond_iface, self._vlan_iface(bond_name, vlan_id_int)]
            spec_interfaces = [
                {"macAddress": m, "name": n} for n, m in zip(nic_names, mac_addresses)
            ]

        nmstate_data = {
            "apiVersion": f"{NMStateConfigCRD.GROUP}/{NMStateConfigCRD.VERSION}",
            "kind": NMStateConfigCRD.KIND,
            "metadata": {
                "labels": {"infraenvs.agent-install.openshift.io": infra_env},
                "name": f"nmstate-config-{name}",
                "namespace": namespace,
            },
            "spec": {
                "config": {"interfaces": config_interfaces},
                "interfaces": spec_interfaces,
            },
        }

        self.validate_yaml_format(nmstate_data)
        self.bmh_logger.info(f"Successfully generated NMStateConfig definition for {name}")
        return nmstate_data
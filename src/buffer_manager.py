import asyncio
import base64
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
import kubernetes
from kubernetes import client

from src.yaml_generators import YamlGenerator
from src.openshift_utils import OpenShiftUtils
from src.unified_server_client import UnifiedServerClient
from src.config import buffer_logger, MAX_AVAILABLE_SERVERS, BUFFER_CHECK_INTERVAL, BMHGenCRD, BMHCRD, Phase
from src.server_profile_config import resolve_nic_and_mac_index

class BufferManager:
    def __init__(self, custom_api: client.CustomObjectsApi = None, core_v1: client.CoreV1Api = None):
        self.custom_api = custom_api or client.CustomObjectsApi()
        self.core_v1 = core_v1 or client.CoreV1Api()

        # Lazy initialization - locks must be created in the event loop that will use them
        self._lock: Optional[asyncio.Lock] = None
        self._shutdown_event: Optional[asyncio.Event] = None

        self.buffer_logger = buffer_logger
        self.MAX_AVAILABLE_SERVERS = MAX_AVAILABLE_SERVERS
        self.BUFFER_CHECK_INTERVAL = BUFFER_CHECK_INTERVAL

    @property
    def lock(self) -> asyncio.Lock:
        """Lazy initialization ensures lock is created in correct event loop"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def shutdown_event(self) -> asyncio.Event:
        """Lazy initialization ensures event is created in correct event loop"""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    def request_shutdown(self):
        """Request graceful shutdown of buffer check loop"""
        if self._shutdown_event is not None:
            self._shutdown_event.set()
            self.buffer_logger.info("Shutdown requested for buffer manager")

    async def get_available_baremetal_hosts(self) -> List[Dict[str, Any]]:
        """
        Query for available BareMetalHosts using run_in_executor for non-blocking I/O.

        A BareMetalHost is considered available if:
        - provisioning.state is empty, "provisioning", or "registering"
        - operationalStatus is not "detached"
        - provisioning.consumer is not set
        """
        self.buffer_logger.debug("Querying for available BareMetalHosts")
        try:
            # Use run_in_executor to prevent blocking the event loop
            loop = asyncio.get_event_loop()
            bmhs = await loop.run_in_executor(
                None,
                lambda: self.custom_api.list_cluster_custom_object(
                    group=BMHCRD.GROUP,
                    version=BMHCRD.VERSION,
                    plural=BMHCRD.PLURAL
                )
            )

            available_bmhs = []
            for bmh in bmhs.get("items", []):
                status = bmh.get("status", {})
                provisioning_state = status.get("provisioning", {}).get("state", "")
                operational_status = status.get("operationalStatus", "")

                if (provisioning_state in ["", "provisioning", "registering"] and
                    operational_status != "detached" and
                    not status.get("provisioning", {}).get("consumer")):
                    available_bmhs.append(bmh)
                    self.buffer_logger.debug(
                        f"BMH {bmh['metadata']['name']} is available "
                        f"(state: {provisioning_state}, operationalStatus: {operational_status})"
                    )
                else:
                    self.buffer_logger.debug(
                        f"BMH {bmh['metadata']['name']} is not available "
                        f"(state: {provisioning_state}, operationalStatus: {operational_status})"
                    )

            self.buffer_logger.info(f"Found {len(available_bmhs)} available BareMetalHosts")
            return available_bmhs
        except Exception as e:
            self.buffer_logger.error(f"Error querying BareMetalHosts: {str(e)}")
            if hasattr(e, 'status') and e.status == 404:
                self.buffer_logger.error("BareMetalHost CRD not found. Ensure that the Metal3 operator is installed.")
                return []
            raise

    async def get_buffered_generators(self) -> List[Dict[str, Any]]:
        """
        Query for buffered BareMetalHostGenerators using run_in_executor.

        Returns generators in "Buffered" phase, sorted by bufferedAt timestamp (FIFO).
        """
        self.buffer_logger.debug("Querying for buffered Generators")
        try:
            # Use run_in_executor to prevent blocking the event loop
            loop = asyncio.get_event_loop()
            bmhgens = await loop.run_in_executor(
                None,
                lambda: self.custom_api.list_cluster_custom_object(
                    group=BMHGenCRD.GROUP,
                    version=BMHGenCRD.VERSION,
                    plural=BMHGenCRD.PLURAL
                )
            )

            buffered = []
            for bmhgen in bmhgens.get("items", []):
                status = bmhgen.get("status", {})
                if status.get("phase") == Phase.BUFFERED:
                    buffered.append(bmhgen)
                    self.buffer_logger.debug(f"Generator {bmhgen['metadata']['name']} is buffered")

            # Sort by bufferedAt timestamp (FIFO)
            buffered.sort(key=lambda x: x.get('status', {}).get('bufferedAt', ''))

            self.buffer_logger.info(f"Found {len(buffered)} buffered Generators")
            return buffered
        except Exception as e:
            self.buffer_logger.error(f"Error querying BareMetalHostGenerators: {str(e)}")
            if hasattr(e, 'status') and e.status == 404:
                self.buffer_logger.error("BareMetalHostGenerator CRD not found. Ensure that the custom operator is installed.")
                return []
            raise

    async def _verify_still_buffered(self, namespace: str, name: str) -> bool:
        """
        Verify generator is still in Buffered phase before processing.

        Prevents race conditions where another process updated the status.
        """
        try:
            loop = asyncio.get_event_loop()
            current_gen = await loop.run_in_executor(
                None,
                lambda: self.custom_api.get_namespaced_custom_object(
                    group=BMHGenCRD.GROUP,
                    version=BMHGenCRD.VERSION,
                    namespace=namespace,
                    plural=BMHGenCRD.PLURAL,
                    name=name
                )
            )
            current_phase = current_gen.get('status', {}).get('phase')
            return current_phase == Phase.BUFFERED
        except Exception as e:
            self.buffer_logger.warning(f"Could not verify phase for {name}: {e}")
            return False

    async def process_buffered_generator(
        self,
        bmhgen: Dict[str, Any],
        unified_client: UnifiedServerClient,
        yaml_generator: YamlGenerator
    ) -> None:
        """Process a single buffered BareMetalHostGenerator"""
        name = bmhgen['metadata']['name']
        namespace = bmhgen['metadata']['namespace']
        server_name = name
        self.buffer_logger.info(f"Processing buffered generator: {name}")

        # Extract per-CR network config overrides (present for spec override feature)
        _nc = (bmhgen.get('spec') or {}).get('networkConfig') or {}
        nic_name_override = _nc.get('nicName') or None
        mac_index_override = _nc.get('macIndex') or None
        effective_nic, effective_mac_index = resolve_nic_and_mac_index(name, nic_name_override, mac_index_override)
        if nic_name_override:
            self.buffer_logger.info(f"[{name}] networkConfig override: nicName={nic_name_override}, macIndex={mac_index_override}")
        else:
            self.buffer_logger.info(f"[{name}] resolved profile: nicName={effective_nic}, macIndex={effective_mac_index}")

        # Safety check: verify this generator is still in Buffered phase
        if not await self._verify_still_buffered(namespace, name):
            self.buffer_logger.warning(f"Generator {name} is no longer in Buffered phase, skipping")
            return

        try:
            # Get the stored server info from status
            status = bmhgen.get('status', {})
            mac_address = status.get('macAddress')
            ipmi_address = status.get('ipmiAddress')
            server_vendor = status.get('serverVendor')

            if not server_vendor:
                annotations = bmhgen.get('metadata', {}).get('annotations', {})
                server_vendor = annotations.get('server_vendor')
                if server_vendor:
                    server_vendor = server_vendor.upper()
                if not server_vendor:
                    # Fallback to default detection logic
                    from src.server_strategy import ServerTypeDetector
                    detected_type = ServerTypeDetector.detect(name)
                    server_vendor = detected_type.value.upper()

            vlan_id = status.get('vlanId')
            if not vlan_id and server_vendor.upper() != 'DELL':
                annotations = bmhgen.get('metadata', {}).get('annotations', {})
                vlan_id = annotations.get('vlanId')
                if not vlan_id:
                    vlan_id = ""

            if not mac_address or not ipmi_address:
                self.buffer_logger.error(f"Missing server info for buffered generator {name}")
                spec = bmhgen.get('spec', {})
                try:
                    server_name = spec.get('serverName', name)
                    # Run blocking network call in thread pool
                    loop = asyncio.get_event_loop()
                    mac_address, ipmi_address = await loop.run_in_executor(
                        None,
                        lambda: unified_client.get_server_info(server_name, server_vendor, mac_index_override)
                    )
                    status_update = {
                        "macAddress": mac_address,
                        "ipmiAddress": ipmi_address
                    }
                    # Run blocking Kubernetes API call in thread pool
                    await loop.run_in_executor(
                        None,
                        lambda: OpenShiftUtils.update_bmh_status(
                            self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                            namespace, BMHGenCRD.PLURAL, name, status_update
                        )
                    )
                except Exception as e:
                    self.buffer_logger.error(f"Error getting server info for {name}: {str(e)}")
                    error_update = {
                        "phase": Phase.FAILED,
                        "message": "Cannot retrieve the server info"
                    }
                    # Run blocking Kubernetes API call in thread pool
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: OpenShiftUtils.update_bmh_status(
                            self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                            namespace, BMHGenCRD.PLURAL, name, error_update
                        )
                    )
                    return

            spec = bmhgen.get('spec', {})
            target_namespace = spec.get('namespace', namespace)
            infra_env = spec.get('infraEnv')

            # Check if BareMetalHost already exists (idempotency check)
            try:
                loop = asyncio.get_event_loop()
                existing_bmh = await loop.run_in_executor(
                    None,
                    lambda: self.custom_api.get_namespaced_custom_object(
                        group=BMHCRD.GROUP,
                        version=BMHCRD.VERSION,
                        namespace=target_namespace,
                        plural=BMHCRD.PLURAL,
                        name=name
                    )
                )
                self.buffer_logger.info(f"BareMetalHost {name} already exists in {target_namespace}, updating status to Completed")

                # BMH already exists, just update the generator status
                completed_status = {
                    "phase": Phase.COMPLETED,
                    "message": f"BareMetalHost {name} already exists (released from buffer)",
                    "bmhName": name,
                    "bmhNamespace": target_namespace,
                    "macAddress": mac_address,
                    "ipmiAddress": ipmi_address,
                    "serverVendor": server_vendor,
                    "vlanId": vlan_id,
                    "selectedNicName": effective_nic,
                    "selectedMacIndex": effective_mac_index,
                }
                # Run blocking Kubernetes API call in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: OpenShiftUtils.update_bmh_status(
                        self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                        namespace, BMHGenCRD.PLURAL, name, completed_status
                    )
                )
                self.buffer_logger.info(f"Updated generator {name} status to Completed (BMH already existed)")
                return
            except Exception as check_error:
                # 404 means BMH doesn't exist yet, which is expected - continue with creation
                if hasattr(check_error, 'status') and check_error.status == 404:
                    self.buffer_logger.debug(f"BareMetalHost {name} does not exist yet, proceeding with creation")
                else:
                    # Unexpected error checking for BMH existence
                    self.buffer_logger.warning(f"Error checking if BareMetalHost exists: {check_error}")

            # Create BMC Secret
            bmc_secret = yaml_generator.generate_bmc_secret(
                name=name,
                namespace=target_namespace,
                server_vendor=server_vendor
            )
            # Run blocking Kubernetes API call in thread pool
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: OpenShiftUtils.create_bmc_secret(self.core_v1, target_namespace, bmc_secret, server_name)
            )

            # Create BareMetalHost
            bmh = yaml_generator.generate_baremetal_host(
                name=name,
                namespace=target_namespace,
                server_vendor=server_vendor,
                mac_address=mac_address,
                ipmi_address=ipmi_address,
                infra_env=infra_env,
                labels=spec.get('labels', {})
            )
            # Run blocking Kubernetes API call in thread pool
            await loop.run_in_executor(
                None,
                lambda: OpenShiftUtils.create_baremetalhost(self.custom_api, target_namespace, bmh, server_name)
            )
            self.buffer_logger.info(f"Created BareMetalHost: {name}")

            # Create NMStateConfig for Dell servers
            if server_vendor and server_vendor.upper() == 'DELL' and vlan_id:
                nmstate_config = yaml_generator.generate_nmstate_config(
                    name=server_name,
                    namespace=target_namespace,
                    macAddress=mac_address,
                    infra_env=infra_env,
                    vlanId=vlan_id,
                    nic_name_override=nic_name_override
                )
                # Run blocking Kubernetes API call in thread pool
                await loop.run_in_executor(
                    None,
                    lambda: OpenShiftUtils.create_nmstate_config(self.custom_api, target_namespace, nmstate_config, server_name)
                )

            # Update generator status to Completed
            completed_status = {
                "phase": Phase.COMPLETED,
                "message": f"Successfully created BareMetalHost {name} (released from buffer)",
                "bmhName": name,
                "bmhNamespace": target_namespace,
                "macAddress": mac_address,
                "ipmiAddress": ipmi_address,
                "serverVendor": server_vendor,
                "vlanId": vlan_id,
                "selectedNicName": effective_nic,
                "selectedMacIndex": effective_mac_index,
            }

            # CRITICAL: Update status to Completed - this must succeed to prevent re-processing
            try:
                # Run blocking Kubernetes API call in thread pool
                await loop.run_in_executor(
                    None,
                    lambda: OpenShiftUtils.update_bmh_status(
                        self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                        namespace, BMHGenCRD.PLURAL, name, completed_status
                    )
                )
                self.buffer_logger.info(f"Updated generator {name} status to Completed")
            except Exception as status_error:
                self.buffer_logger.error(f"CRITICAL: Failed to update generator {name} status to Completed: {status_error}")
                raise

        except Exception as e:
            self.buffer_logger.error(f"Error processing buffered generator {name}: {str(e)}")
            # Try to mark as Failed to prevent infinite retry loop
            try:
                error_status = {
                    "phase": Phase.FAILED,
                    "message": f"Error releasing from buffer: {str(e)}"
                }
                # Run blocking Kubernetes API call in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: OpenShiftUtils.update_bmh_status(
                        self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                        namespace, BMHGenCRD.PLURAL, name, error_status
                    )
                )
                self.buffer_logger.info(f"Marked generator {name} as Failed to prevent retry loop")
            except Exception as final_error:
                self.buffer_logger.critical(f"Could not update {name} to Failed status: {final_error}")
            raise

    async def buffer_check_iteration(
        self,
        unified_client: UnifiedServerClient,
        yaml_generator: YamlGenerator
    ) -> Dict[str, int]:
        """
        Single iteration of buffer check - used by kopf daemon.

        Returns statistics about the check iteration.
        """
        stats = {
            "available_count": 0,
            "buffered_count": 0,
            "released_count": 0,
            "failed_count": 0
        }

        async with self.lock:
            self.buffer_logger.info("Running buffer check iteration")

            available_bmhs = await self.get_available_baremetal_hosts()
            stats["available_count"] = len(available_bmhs)

            self.buffer_logger.info(f"Available BareMetalHosts: {stats['available_count']}/{self.MAX_AVAILABLE_SERVERS}")

            # Always query buffered generators for accurate reporting
            buffered = await self.get_buffered_generators()
            stats["buffered_count"] = len(buffered)

            if stats["available_count"] < self.MAX_AVAILABLE_SERVERS:
                slots_available = self.MAX_AVAILABLE_SERVERS - stats["available_count"]

                for i, bmhgen in enumerate(buffered[:slots_available]):
                    gen_name = bmhgen['metadata']['name']
                    self.buffer_logger.info(f"Releasing buffered generator {gen_name} from buffer")
                    try:
                        await self.process_buffered_generator(bmhgen, unified_client, yaml_generator)
                        stats["released_count"] += 1
                    except Exception as process_error:
                        self.buffer_logger.error(f"Failed to process buffered generator {gen_name}: {process_error}")
                        stats["failed_count"] += 1
                        # Continue with next generator - don't let one failure stop the whole loop
                        continue

                    # Small delay between releases to avoid overwhelming the API server
                    if i < slots_available - 1:
                        await asyncio.sleep(2)
            else:
                self.buffer_logger.info(f"No slots available to release servers from buffer (buffered={stats['buffered_count']})")

        return stats

    async def is_to_buffer(
        self,
        server_name: str,
        mac_address: str,
        ipmi_address: str,
        server_vendor: str,
        vlan_id: str,
        namespace: str,
        name: str
    ) -> bool:
        """
        Check if server should be buffered or created immediately.

        Thread-safe: Uses asyncio.Lock to coordinate with kopf daemon.
        """
        async with self.lock:
            available_bmhs = await self.get_available_baremetal_hosts()
            available_count = len(available_bmhs)

            self.buffer_logger.info(f"Current available BareMetalHosts: {available_count}/{self.MAX_AVAILABLE_SERVERS}")

            if available_count >= self.MAX_AVAILABLE_SERVERS:
                self.buffer_logger.info(f"Buffering server {server_name} as buffer is full")
                try:
                    status_update = {
                        "phase": Phase.BUFFERED,
                        "message": f"Server buffered (available: {available_count}/{self.MAX_AVAILABLE_SERVERS})",
                        "bufferedAt": datetime.utcnow().isoformat() + "Z",
                        "macAddress": mac_address,
                        "ipmiAddress": ipmi_address,
                        "serverVendor": server_vendor,
                        "vlanId": vlan_id
                    }
                    # Run blocking Kubernetes API call in thread pool
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: OpenShiftUtils.update_bmh_status(
                            self.custom_api, BMHGenCRD.GROUP, BMHGenCRD.VERSION,
                            namespace, BMHGenCRD.PLURAL, name, status_update
                        )
                    )
                    self.buffer_logger.info(f"Buffered server {server_name} - limit reached")
                except Exception as e:
                    self.buffer_logger.error(f"Error updating status to Buffered for {server_name}: {str(e)}")
                return True
            else:
                self.buffer_logger.info(f"Creating BareMetalHost immediately - room available {available_count}/{self.MAX_AVAILABLE_SERVERS}")
                return False

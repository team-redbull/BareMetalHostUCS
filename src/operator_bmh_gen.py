import asyncio
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, Optional
import logging
import kopf
import kubernetes
from kubernetes import client, config
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

from src.buffer_manager import BufferManager
from src.openshift_utils import OpenShiftUtils
from src.yaml_generators import YamlGenerator
from src.unified_server_client import UnifiedServerClient, initialize_unified_client
from src.config import operator_logger, buffer_logger, BUFFER_CHECK_INTERVAL, BMHGenCRD, BMHCRD, Phase
from src.server_profile_config import resolve_nic_and_mac_index

# Initialize YAML generator
yaml_generator = YamlGenerator()

# Load Kubernetes configuration
try:
    config.load_incluster_config()
    operator_logger.info("Loaded in-cluster Kubernetes configuration.")
except Exception as e:
    operator_logger.warning(f"Failed to load in cluster config: {e}. ")
    operator_logger.info("Falling back to kubeconfig file.")
    config.load_kube_config()

# Initialize Kubernetes clients
k8s_client = client.ApiClient()
custom_api = client.CustomObjectsApi(k8s_client)
core_api = client.CoreV1Api(k8s_client)
operator_logger.info("Kubernetes client initialized.")

# Initialize buffer manager and unified client (will be set in startup)
buffer_manager = BufferManager(custom_api, core_api)
unified_client: Optional[UnifiedServerClient] = None

# Background task for buffer checking (created in startup)
_buffer_check_task: Optional[asyncio.Task] = None

# Disable SSL warnings
disable_warnings(InsecureRequestWarning)


def _extract_network_override(spec: Dict[str, Any], server_name: str):
    """
    Return (nic_name_override, mac_index_override) from spec.networkConfig.

    Returns (None, None) when networkConfig is absent.
    Raises kopf.PermanentError when only one of the two fields is set.
    """
    nc = spec.get('networkConfig') or {}
    nic = nc.get('nicName') or None
    idx = nc.get('macIndex') or None
    if bool(nic) != bool(idx):
        raise kopf.PermanentError(
            f"[{server_name}] spec.networkConfig requires both nicName and macIndex together, "
            f"got nicName={nic!r}, macIndex={idx!r}"
        )
    return nic, idx


@kopf.on.startup()
async def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings and initialize connections"""
    operator_logger.info("Starting operator configuration.")

    # Configure kopf settings
    # Serial processing (1 worker) for maximum stability and strict buffer control
    # This prevents race conditions and ensures the operator stays responsive
    settings.execution.max_workers = 1  # Process one handler at a time
    settings.execution.idle_timeout = 60  # Timeout for idle handlers (seconds)

    settings.posting.enabled = False

    # Batching settings - single worker for strict serialization
    settings.batching.worker_limit = 1  # Process one resource at a time
    settings.batching.batch_window = 1  # Wait up to 1 second to batch events
    settings.batching.idle_timeout = 5  # Timeout for idle batches (seconds)

    # Watch settings - handle large numbers of resources
    settings.watching.server_timeout = 60  # Watch connection timeout (seconds)
    settings.watching.client_timeout = 60  # Client timeout (seconds)
    settings.watching.reconnect_backoff = 1  # Reconnect backoff (seconds)
    
    settings.persistence.finalizer = BMHGenCRD.FINALIZER
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage()

    operator_logger.info("Operator settings configured for serial processing with asyncio.to_thread() for non-blocking I/O.")

    # Test API access
    try:
        operator_logger.info("Testing API access...")
        custom_api.get_api_resources()
        operator_logger.info("Successfully accessed Kubernetes API.")

        custom_api.list_cluster_custom_object(
            group=BMHGenCRD.GROUP,
            version=BMHGenCRD.VERSION,
            plural=BMHGenCRD.PLURAL
        )
        operator_logger.info("Successfully accessed BareMetalHostGenerator custom resources.")
    except Exception as e:
        operator_logger.error(f"Failed to access Kubernetes API or custom resources: {e}")

    # Initialize unified client
    global unified_client
    unified_client = initialize_unified_client()

    # Check initial available BareMetalHost count
    try:
        available_bmhs = await buffer_manager.get_available_baremetal_hosts()
        available_count = len(available_bmhs)

        if available_count > buffer_manager.MAX_AVAILABLE_SERVERS:
            operator_logger.warning(
                f"Starting with {available_count} available servers, "
                f"exceeds limit of {buffer_manager.MAX_AVAILABLE_SERVERS}."
            )
            operator_logger.warning("New servers will be buffered until available count drops below the limit.")
        else:
            operator_logger.info(
                f"Starting with {available_count}/{buffer_manager.MAX_AVAILABLE_SERVERS} available servers."
            )
    except Exception as e:
        operator_logger.error(f"Error during initial available BareMetalHost check: {e}")

    operator_logger.info("Operator configuration completed successfully.")

    # Create background task for buffer checking
    global _buffer_check_task
    _buffer_check_task = asyncio.create_task(_buffer_check_loop(), name="buffer-check-loop")
    operator_logger.info("Started background buffer check task")


async def _buffer_check_loop():
    """
    Background task that periodically checks for buffered servers to release.

    This runs as a single global task created in startup, ensuring:
    - Only ONE task runs regardless of number of resources
    - asyncio.Lock works correctly (created in same event loop)
    - Graceful cancellation on operator shutdown
    """
    buffer_logger.info("Buffer check loop starting, waiting 10 seconds before first check...")
    await asyncio.sleep(10)

    while True:
        try:
            # Skip if unified client not initialized yet
            if unified_client is None:
                buffer_logger.warning("Unified client not initialized, skipping buffer check")
                await asyncio.sleep(BUFFER_CHECK_INTERVAL)
                continue

            # Perform buffer check iteration
            stats = await buffer_manager.buffer_check_iteration(unified_client, yaml_generator)
            buffer_logger.info(
                f"Buffer check completed: "
                f"available={stats['available_count']}, "
                f"buffered={stats['buffered_count']}, "
                f"released={stats['released_count']}, "
                f"failed={stats['failed_count']}"
            )

        except asyncio.CancelledError:
            buffer_logger.info("Buffer check loop cancelled, exiting gracefully")
            raise  # Re-raise to properly exit the task
        except Exception as e:
            buffer_logger.error(f"Error in buffer check iteration: {e}", exc_info=True)
            # Continue loop on errors - don't crash the background task

        # Wait before next iteration
        await asyncio.sleep(BUFFER_CHECK_INTERVAL)


@kopf.on.create(BMHGenCRD.GROUP, BMHGenCRD.VERSION, BMHGenCRD.PLURAL)
async def create_bmh(spec: Dict[str, Any], name: str, namespace: str, annotations: Optional[Dict[str, str]], patch: kopf.Patch, **kwargs):
    """
    Handler for creating BareMetalHost resources from BareMetalHostGenerator CRDs.

    This handler:
    1. Queries the management system for server info (MAC, IP)
    2. Checks if the server should be buffered or created immediately
    3. Creates BMC Secret, BareMetalHost, and optionally NMStateConfig
    4. Updates the generator status
    """
    handler_start_time = time.time()
    operator_logger.info(f"[CREATE] Starting handler for BareMetalHostGenerator: {name} in namespace: {namespace}")

    # Get or default serverName
    server_name = spec.get('serverName', name)
    target_namespace = spec.get('namespace', namespace)
    infra_env = spec.get('infraEnv')

    # If serverName was not provided in spec, patch it to make the spec explicit
    if 'serverName' not in spec:
        operator_logger.info(f"serverName not provided in spec, patching with CR name: {name}")
        try:
            patch_body = {
                "spec": {
                    "serverName": name
                }
            }
            # Run blocking call in thread pool to avoid blocking event loop
            await asyncio.to_thread(
                custom_api.patch_namespaced_custom_object,
                group=BMHGenCRD.GROUP,
                version=BMHGenCRD.VERSION,
                namespace=namespace,
                plural=BMHGenCRD.PLURAL,
                name=name,
                body=patch_body
            )
            operator_logger.info(f"Successfully patched serverName to spec: {name}")
        except Exception as e:
            operator_logger.warning(f"Failed to patch serverName to spec (non-critical): {e}")
            # Continue anyway - this is just for clarity, not functionality

    metadata = kwargs.get('metadata', {})
    operator_logger.info(f"Metadata for BMHG {name}: {metadata}")

    server_vendor = (annotations.get('server_vendor') if annotations else None)
    if server_vendor:
        server_vendor = server_vendor.upper()
    vlan_id = annotations.get('vlanId') if annotations else None
    nic_name_override, mac_index_override = _extract_network_override(spec, server_name)
    effective_nic, effective_mac_index = resolve_nic_and_mac_index(server_name, nic_name_override, mac_index_override)
    if nic_name_override:
        operator_logger.info(f"[{server_name}] networkConfig override: nicName={nic_name_override}, macIndex={mac_index_override}")
    else:
        operator_logger.info(f"[{server_name}] resolved profile: nicName={effective_nic}, macIndex={effective_mac_index}")

    operator_logger.info(f"Server vendor annotation: {server_vendor}")

    if not infra_env:
        # Update status before raising error using kopf's patch mechanism
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = f"infraEnv is required in the spec of BareMetalHostGenerator {name}"
        raise kopf.PermanentError(f"infraEnv is required in the spec of BareMetalHostGenerator {name}")

    # Validate unified_client before proceeding
    if unified_client is None:
        operator_logger.error("Unified client not initialized - cannot process server info")
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "Operator not ready: Unified client not initialized"
        raise kopf.PermanentError("Unified client not initialized")

    # Update status to Processing - only after validation
    patch.status["phase"] = Phase.PROCESSING
    patch.status["message"] = f"Looking up server {server_name} in management systems."

    try:
        operator_logger.info(f"Searching for server info: {server_name}")
        # Run blocking network call in thread pool with timeout to avoid blocking event loop
        # Timeout after 60 seconds to prevent hanging handlers
        try:
            mac_address, ip_address = await asyncio.wait_for(
                asyncio.to_thread(
                    unified_client.get_server_info,
                    server_name,
                    server_vendor,
                    mac_index_override
                ),
                timeout=60.0  # 60 second timeout
            )
        except asyncio.TimeoutError:
            operator_logger.error(f"Timeout getting server info for {server_name} after 60 seconds")
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Timeout getting server info for {server_name} - request took longer than 60 seconds"
            raise kopf.PermanentError(f"Timeout getting server info for {server_name}")

        if not mac_address or not ip_address:
            # Update status before raising error
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Server {server_name} not found in any management system"
            raise kopf.PermanentError(f"Server {server_name} not found in any management system")

        operator_logger.info(f"Found server {server_name} with MAC: {mac_address}, IP: {ip_address}")

        if not server_vendor:
            detected_type = unified_client._detector.detect(server_name)
            server_vendor = detected_type.name
        operator_logger.info(f"Final server vendor: {server_vendor}")

        # Check if should buffer or create immediately
        if await buffer_manager.is_to_buffer(
            server_name,
            mac_address,
            ip_address,
            server_vendor,
            vlan_id,
            namespace,
            name
        ):
            # Server was buffered - MUST update patch.status to prevent Kopf from
            # overwriting the "Buffered" status with "Processing" when handler returns
            patch.status["phase"] = Phase.BUFFERED
            patch.status["message"] = f"Server buffered - limit reached ({buffer_manager.MAX_AVAILABLE_SERVERS} available)"
            patch.status["bufferedAt"] = datetime.utcnow().isoformat() + "Z"
            patch.status["macAddress"] = mac_address
            patch.status["ipmiAddress"] = ip_address
            patch.status["serverVendor"] = server_vendor
            if vlan_id:
                patch.status["vlanId"] = vlan_id
            return

        # Create resources immediately
        bmh_secret = yaml_generator.generate_bmc_secret(
            name=server_name,
            namespace=target_namespace,
            server_vendor=server_vendor
        )
        # Run blocking Kubernetes API call in thread pool
        await asyncio.to_thread(
            OpenShiftUtils.create_bmc_secret,
            core_api, target_namespace, bmh_secret, server_name
        )

        bmh = yaml_generator.generate_baremetal_host(
            name=server_name,
            namespace=target_namespace,
            server_vendor=server_vendor,
            mac_address=mac_address,
            ipmi_address=ip_address,
            infra_env=infra_env,
            labels=spec.get('labels')
        )
        # Run blocking Kubernetes API call in thread pool
        await asyncio.to_thread(
            OpenShiftUtils.create_baremetalhost,
            custom_api, target_namespace, bmh, server_name
        )

        # Create NMStateConfig for Dell servers
        if server_vendor:
            operator_logger.info(f"The VLAN ID is: {vlan_id}")
            if server_vendor.upper() == "DELL" and vlan_id:
                nmstate_config = yaml_generator.generate_nmstate_config(
                    name=server_name,
                    namespace=target_namespace,
                    macAddress=mac_address,
                    infra_env=infra_env,
                    vlanId=vlan_id,
                    nic_name_override=nic_name_override
                )
                # Run blocking Kubernetes API call in thread pool
                await asyncio.to_thread(
                    OpenShiftUtils.create_nmstate_config,
                    custom_api, target_namespace, nmstate_config, server_name
                )

        # Update status to Completed
        patch.status["phase"] = Phase.COMPLETED
        patch.status["message"] = f"Successfully created BareMetalHost {server_name}"
        patch.status["bmhName"] = server_name
        patch.status["bmhNamespace"] = target_namespace
        patch.status["macAddress"] = mac_address
        patch.status["ipmiAddress"] = ip_address
        patch.status["serverVendor"] = server_vendor
        patch.status["vlanId"] = vlan_id
        patch.status["selectedNicName"] = effective_nic
        patch.status["selectedMacIndex"] = effective_mac_index

        handler_duration = time.time() - handler_start_time
        operator_logger.info(f"[CREATE] Successfully completed BareMetalHost creation for: {server_name} (took {handler_duration:.2f}s)")

    except kopf.PermanentError:
        # Re-raise PermanentError as-is (status already updated)
        raise
    except Exception as e:
        handler_duration = time.time() - handler_start_time
        operator_logger.error(f"[CREATE] Error processing BareMetalHostGenerator {name} after {handler_duration:.2f}s: {e}", exc_info=True)

        # Update status using kopf's patch mechanism - ensure it's marked as FAILED
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = str(e)

        # Preserve MAC/IP if they were retrieved before error
        if 'mac_address' in locals() and mac_address:
            patch.status["macAddress"] = mac_address
        if 'ip_address' in locals() and ip_address:
            patch.status["ipmiAddress"] = ip_address
        if 'server_vendor' in locals() and server_vendor:
            patch.status["serverVendor"] = server_vendor

        raise kopf.PermanentError(f"Failed to create BareMetalHost for {server_name}: {e}")


@kopf.on.update(BMHGenCRD.GROUP, BMHGenCRD.VERSION, BMHGenCRD.PLURAL)
async def update_bmh(spec, status, name, namespace, annotations, patch, **kwargs):
    """
    Handler for updates to BareMetalHostGenerator.

    Supports redeploy annotation: Set redeploy="true" to trigger recreation of resources.
    Redeploy only works for resources in "Completed" or "Failed" phase.
    """
    operator_logger.info(f"[UPDATE] BareMetalHostGenerator {name} update triggered")

    # Check for redeploy annotation
    if annotations and annotations.get('redeploy') == 'true':
        operator_logger.info(f"[UPDATE] Redeploy annotation detected for: {name}")

        # Validate phase
        current_phase = status.get('phase')
        if current_phase not in [Phase.COMPLETED, Phase.FAILED]:
            operator_logger.warning(
                f"[UPDATE] Redeploy requested for {name} but phase is '{current_phase}'. "
                f"Redeploy only works for Completed or Failed phases."
            )
            return status

        # Execute redeploy logic
        operator_logger.info(f"[UPDATE] Starting redeploy for {name} (current phase: {current_phase})")
        await redeploy_bmh_resources(spec, status, name, namespace, annotations, patch)
    else:
        operator_logger.debug(f"[UPDATE] BareMetalHostGenerator {name} update ignored - no redeploy annotation")

    operator_logger.debug(f"[UPDATE] Current status: {status}")
    return status


# ============================================================================
# Redeploy Helper Functions
# ============================================================================

async def redeploy_bmh_resources(spec, status, name, namespace, annotations, patch):
    """
    Redeploy BareMetalHost and related resources.

    Steps:
    1. Delete existing resources (BMH, Secret, NMStateConfig)
    2. Re-query server info from management system
    3. Recreate resources
    4. Remove redeploy annotation
    5. Update status
    """
    handler_start_time = time.time()
    operator_logger.info(f"[REDEPLOY] Starting redeploy for: {name} in namespace: {namespace}")

    try:
        # Update status to Processing
        patch.status["phase"] = Phase.PROCESSING
        patch.status["message"] = "Redeploying resources..."

        # Step 1: Delete existing resources
        operator_logger.info(f"[REDEPLOY] Deleting existing resources for: {name}")
        await delete_existing_resources(status, name, namespace)

        # Step 2: Re-query server info from management system
        server_name = spec.get('serverName', name)
        server_vendor = annotations.get('server_vendor') if annotations else None
        if server_vendor:
            server_vendor = server_vendor.upper()
        nic_name_override, mac_index_override = _extract_network_override(spec, server_name)
        effective_nic, effective_mac_index = resolve_nic_and_mac_index(server_name, nic_name_override, mac_index_override)
        if nic_name_override:
            operator_logger.info(f"[REDEPLOY] [{server_name}] networkConfig override: nicName={nic_name_override}, macIndex={mac_index_override}")

        operator_logger.info(f"[REDEPLOY] Querying server info for: {server_name}")

        # Check if unified_client is available
        if unified_client is None:
            raise kopf.PermanentError("Unified client not initialized - cannot query server info")

        # Use global unified client to get fresh server info (run in thread to avoid blocking)
        mac_address, ipmi_address = await asyncio.to_thread(
            unified_client.get_server_info, server_name, server_vendor, mac_index_override
        )

        if not mac_address or not ipmi_address:
            raise kopf.PermanentError(f"Failed to retrieve server info for {server_name}")

        operator_logger.info(f"[REDEPLOY] Retrieved server info - MAC: {mac_address}, IP: {ipmi_address}")

        # Step 3: Recreate resources
        operator_logger.info(f"[REDEPLOY] Creating new resources for: {name}")
        await create_bmh_resources(spec, name, namespace, mac_address, ipmi_address,
                                   server_vendor, annotations, patch, nic_name_override)

        # Step 4: Remove redeploy annotation
        operator_logger.info(f"[REDEPLOY] Removing redeploy annotation from: {name}")
        await remove_redeploy_annotation(name, namespace)

        # Step 5: Update status to Completed
        patch.status["phase"] = Phase.COMPLETED
        patch.status["message"] = "Resources successfully redeployed"
        patch.status["selectedNicName"] = effective_nic
        patch.status["selectedMacIndex"] = effective_mac_index

        handler_duration = time.time() - handler_start_time
        operator_logger.info(f"[REDEPLOY] Successfully completed redeploy for: {name} (took {handler_duration:.2f}s)")

    except Exception as e:
        handler_duration = time.time() - handler_start_time
        operator_logger.error(f"[REDEPLOY] Error during redeploy for {name} after {handler_duration:.2f}s: {e}", exc_info=True)

        # Remove annotation even on failure to prevent retries
        try:
            await remove_redeploy_annotation(name, namespace)
        except Exception as remove_error:
            operator_logger.error(f"[REDEPLOY] Failed to remove annotation after error: {remove_error}")

        # Update status to Failed
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = f"Redeploy failed: {str(e)}"

        raise kopf.PermanentError(f"Redeploy failed: {str(e)}")


async def delete_existing_resources(status, name, namespace):
    """
    Delete BMH, Secret, and NMStateConfig if they exist.

    Note: NMStateConfig is only deleted for Dell servers (DELL vendor)
    """
    bmh_name = status.get('bmhName')
    bmh_namespace = status.get('bmhNamespace')
    server_vendor = status.get('serverVendor')

    if not bmh_name or not bmh_namespace:
        operator_logger.warning(f"[REDEPLOY] No existing resources found in status for {name}")
        return

    operator_logger.info(f"[REDEPLOY] Deleting resources: BMH={bmh_name}, Namespace={bmh_namespace}, Vendor={server_vendor}")

    # Delete NMStateConfig (Dell servers only - other vendors don't use it)
    if server_vendor and server_vendor.upper() == 'DELL':
        await asyncio.to_thread(
            OpenShiftUtils.delete_nmstate_config,
            custom_api, bmh_namespace, bmh_name
        )
        operator_logger.info(f"[REDEPLOY] Deleted NMStateConfig: {bmh_name} (Dell server)")
    else:
        operator_logger.debug(f"[REDEPLOY] Skipping NMStateConfig deletion for {server_vendor} server")

    # Delete Secret (all vendors)
    secret_name = f"{server_vendor.lower()}-cred-{bmh_name}" if server_vendor else f"bmc-cred-{bmh_name}"
    await asyncio.to_thread(
        OpenShiftUtils.delete_bmc_secret,
        core_api, bmh_namespace, secret_name
    )
    operator_logger.info(f"[REDEPLOY] Deleted Secret: {secret_name}")

    # Delete BMH (all vendors)
    await asyncio.to_thread(
        OpenShiftUtils.delete_baremetalhost,
        custom_api, bmh_namespace, bmh_name
    )
    operator_logger.info(f"[REDEPLOY] Deleted BareMetalHost: {bmh_name}")


async def remove_redeploy_annotation(name, namespace):
    """Remove the redeploy annotation from BMHGen to prevent infinite loops.

    Uses JSON merge patch with null value to actually delete the annotation.
    Simply omitting a key from a patch body doesn't remove it - we must
    explicitly set it to None (null in JSON) to delete it.
    """
    try:
        # To remove an annotation with JSON merge patch, set it to None (null)
        # This tells Kubernetes to delete this specific annotation key
        patch_body = {
            "metadata": {
                "annotations": {
                    "redeploy": None  # Setting to None removes the annotation
                }
            }
        }

        await asyncio.to_thread(
            lambda: custom_api.patch_namespaced_custom_object(
                BMHGenCRD.GROUP, BMHGenCRD.VERSION, namespace, BMHGenCRD.PLURAL,
                name, patch_body
            )
        )
        operator_logger.info(f"[REDEPLOY] Removed redeploy annotation from {name}")
    except Exception as e:
        operator_logger.error(f"[REDEPLOY] Failed to remove redeploy annotation from {name}: {e}")
        # Don't raise - this is a cleanup operation


async def create_bmh_resources(spec, name, namespace, mac_address, ipmi_address,
                               server_vendor, annotations, patch, nic_name_override=None):
    """
    Create BMH, Secret, and NMStateConfig resources.

    Note: NMStateConfig is only created for Dell servers with vlanId annotation
    """
    target_namespace = spec.get('namespace', namespace)
    infra_env = spec.get('infraEnv')
    labels = spec.get('labels', {})
    vlan_id = annotations.get('vlanId') if annotations else None

    operator_logger.info(f"[REDEPLOY] Creating resources in namespace: {target_namespace}")

    # Generate and create Secret
    yaml_generator = YamlGenerator()
    bmc_secret = yaml_generator.generate_bmc_secret(name, target_namespace, server_vendor)
    await asyncio.to_thread(
        OpenShiftUtils.create_bmc_secret, core_api, target_namespace, bmc_secret, name
    )
    operator_logger.info(f"[REDEPLOY] Created BMC Secret for {name}")

    # Generate and create BMH
    bmh_data = yaml_generator.generate_baremetal_host(
        name, target_namespace, server_vendor, mac_address, ipmi_address, infra_env, labels
    )
    await asyncio.to_thread(
        OpenShiftUtils.create_baremetalhost, custom_api, target_namespace, bmh_data, name
    )
    operator_logger.info(f"[REDEPLOY] Created BareMetalHost: {name}")

    # Generate and create NMStateConfig (Dell servers only with VLAN ID)
    if server_vendor and server_vendor.upper() == "DELL" and vlan_id:
        operator_logger.info(f"[REDEPLOY] Creating NMStateConfig for Dell server {name} with VLAN {vlan_id}")
        nmstate_data = yaml_generator.generate_nmstate_config(
            name, target_namespace, mac_address, infra_env, vlan_id,
            nic_name_override=nic_name_override
        )
        await asyncio.to_thread(
            OpenShiftUtils.create_nmstate_config, custom_api, target_namespace, nmstate_data, name
        )
        operator_logger.info(f"[REDEPLOY] Created NMStateConfig for {name}")
    else:
        operator_logger.debug(f"[REDEPLOY] Skipping NMStateConfig creation for {server_vendor} server")

    # Update status with resource info
    patch.status["bmhName"] = name
    patch.status["bmhNamespace"] = target_namespace
    patch.status["macAddress"] = mac_address
    patch.status["ipmiAddress"] = ipmi_address
    patch.status["serverVendor"] = server_vendor
    if vlan_id:
        patch.status["vlanId"] = vlan_id


@kopf.on.delete(BMHGenCRD.GROUP, BMHGenCRD.VERSION, BMHGenCRD.PLURAL)
async def delete_bmh(spec, name, namespace, status, **kwargs):
    """
    Handler for deleting BareMetalHostGenerator CRDs.

    This handler cleans up associated resources based on DELETE_RESOURCES_ON_DELETE configuration:
    - NMStateConfig (for Dell servers)
    - BMC Secret
    - BareMetalHost

    If DELETE_RESOURCES_ON_DELETE is false, resources are preserved.
    """
    handler_start_time = time.time()
    operator_logger.info(f"[DELETE] Starting handler for BareMetalHost Generator: {name} in namespace: {namespace}")

    from src.config import DELETE_RESOURCES_ON_DELETE

    if not DELETE_RESOURCES_ON_DELETE:
        operator_logger.info(f"[DELETE] DELETE_RESOURCES_ON_DELETE is disabled - preserving resources for: {name}")
        handler_duration = time.time() - handler_start_time
        operator_logger.info(f"[DELETE] Completed (resources preserved) for: {name} (took {handler_duration:.2f}s)")
        return

    if status.get('bmhName') and status.get('bmhNamespace'):
        bmh_name = status['bmhName']
        bmh_namespace = status['bmhNamespace']
        server_vendor = status.get('serverVendor')
        vlan_id = status.get('vlanId')

        try:
            # Delete NMStateConfig if exists (Dell servers only)
            if server_vendor and server_vendor.upper() == 'DELL':
                nmstate_config_name = bmh_name
                # Run blocking Kubernetes API call in thread pool
                await asyncio.to_thread(
                    OpenShiftUtils.delete_nmstate_config,
                    custom_api, bmh_namespace, nmstate_config_name
                )
                operator_logger.info(
                    f"Deleted NMStateConfig: {nmstate_config_name} in namespace: {bmh_namespace}"
                )

            # Delete BMC Secret
            bmc_secret_name = f"{server_vendor.lower()}-cred-{bmh_name}" if server_vendor else f"bmc-cred-{bmh_name}"
            # Run blocking Kubernetes API call in thread pool
            await asyncio.to_thread(
                OpenShiftUtils.delete_bmc_secret,
                core_api, bmh_namespace, bmc_secret_name
            )
            operator_logger.info(
                f"Deleted BMC Secret: {bmc_secret_name} in namespace: {bmh_namespace}"
            )

            # Delete BareMetalHost
            # Run blocking Kubernetes API call in thread pool
            await asyncio.to_thread(
                OpenShiftUtils.delete_baremetalhost,
                custom_api, bmh_namespace, bmh_name
            )
            operator_logger.info(
                f"Deleted BareMetalHost: {bmh_name} in namespace: {bmh_namespace}"
            )

            handler_duration = time.time() - handler_start_time
            operator_logger.info(f"[DELETE] Successfully completed deletion for: {name} (took {handler_duration:.2f}s)")

        except Exception as e:
            handler_duration = time.time() - handler_start_time
            operator_logger.error(f"[DELETE] Error deleting BareMetalHost and related resources for {name} after {handler_duration:.2f}s: {str(e)}", exc_info=True)
            raise kopf.PermanentError(f"Error deleting BareMetalHost and related resources: {str(e)}")
    else:
        handler_duration = time.time() - handler_start_time
        operator_logger.warning(f"[DELETE] No associated BareMetalHost found for generator: {name} (took {handler_duration:.2f}s)")


@kopf.on.cleanup()
async def cleanup_fn(**kwargs):
    """
    Cleanup function called on operator shutdown.

    This handler:
    - Cancels background buffer check task
    - Requests shutdown of buffer manager
    - Disconnects from server management systems
    """
    operator_logger.info("Operator shutting down, cleaning up resources")

    # Cancel background buffer check task
    global _buffer_check_task
    if _buffer_check_task and not _buffer_check_task.done():
        operator_logger.info("Cancelling buffer check task...")
        _buffer_check_task.cancel()
        try:
            await _buffer_check_task
        except asyncio.CancelledError:
            operator_logger.info("Buffer check task cancelled successfully")

    # Request buffer manager shutdown
    buffer_manager.request_shutdown()

    # Disconnect from server management systems
    if unified_client:
        try:
            unified_client.disconnect()
            operator_logger.info("Disconnected from server management systems")
        except Exception as e:
            operator_logger.warning(f"Error disconnecting from server management systems: {e}")

    operator_logger.info("Cleanup completed")


if __name__ == "__main__":
    operator_logger.info("Starting BareMetalHost Generator Operator with Buffering")
    operator_logger.info(f"Process PID: {os.getpid()}")
    operator_logger.info(f"Python version: {subprocess.check_output(['python', '--version']).decode().strip()}")

    try:
        kopf.run()
    except Exception as e:
        operator_logger.critical(f"Operator crashed: {str(e)}")
        operator_logger.exception("Full crash details:")
        raise

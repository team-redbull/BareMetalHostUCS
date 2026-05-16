# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Kubernetes operator that automatically creates BareMetalHost resources by querying multiple server management systems (HP OneView, Cisco UCS Central, and Dell OpenManage Enterprise). The operator bridges vendor-specific management systems with Metal3/OpenShift bare metal deployments.

## Architecture

### Core Components

0. **Configuration Module**: `src/config.py` ⭐ NEW
   - **Centralized configuration** for all components
   - Environment variable management with defaults
   - **Vendor-specific BMC credentials** (HP/Dell/Cisco)
   - Logging configuration with LOG_LEVEL support
   - Buffer management constants (MAX_AVAILABLE_SERVERS, BUFFER_CHECK_INTERVAL)
   - BMC address format helpers
   - Configuration validation
   - **See [src/CONFIG_README.md](src/CONFIG_README.md) for detailed usage**

1. **Operator Entry Point**: `src/operator_bmh_gen.py`
   - Kopf-based Kubernetes operator that watches BareMetalHostGenerator CRDs
   - Handles resource creation, updates, and deletion
   - Implements buffering logic to prevent resource exhaustion
   - Coordinates between UnifiedServerClient and Kubernetes APIs

2. **Strategy Pattern Implementation** (New Architecture):
   - `src/server_strategy.py`: Abstract base class and factory for vendor strategies
   - `src/hp_server_stategy.py`: HP OneView integration
   - `src/ucs_server_strategy.py`: Cisco UCS Central/Manager integration
   - `src/dell_server_strategy.py`: Dell OME integration
   - Each strategy implements: `is_configured()`, `ensure_connected()`, `get_server_info()`, `disconnect()`

3. **Unified Client** (Legacy): `src/unified_server_client.py`
   - Older monolithic implementation still present in operator
   - May be in transition to strategy pattern - check before modifying

4. **Buffer Manager**: `src/buffer_manager.py`
   - Controls the number of available BareMetalHosts in the cluster (default: 20)
   - FIFO queue for buffered servers
   - Periodic checks every 30 seconds to release buffered servers

5. **YAML Generators**: `src/yaml_generators.py`
   - Creates BareMetalHost resource definitions
   - Generates BMC secrets with vendor-specific naming
   - Vendor-specific BMC address formats:
     - HP: `redfish-virtualmedia://{ip}/redfish/v1/Systems/1`
     - Dell: `idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1`
     - Cisco: `ipmi://{ip}`

6. **OpenShift Utilities**: `src/openshift_utils.py`
   - Helper functions for creating and updating Kubernetes resources
   - Status update patterns for BareMetalHostGenerator CRD

### Vendor Detection Logic

The operator detects server vendor in this priority order:
1. **`spec.server_vendor`**: `HP`, `DELL`, or `CISCO` (case-insensitive; validated by CRD schema)
2. **Name-based heuristics** (when `spec.server_vendor` is omitted):
   - Contains `hp` → HP
   - Contains `dell` → Dell
   - Contains `cisco` → Cisco
   - Default → Cisco

### Custom Resource Definition

**Group**: `infra.example.com`
**Version**: `v1alpha1`
**Kind**: `BareMetalHostGenerator`

**Required Fields**:
- `spec.infraEnv`: InfraEnv name for OpenShift Agent-based installation

**Optional Fields**:
- `spec.serverName`: Server name in management system (defaults to CR name)
- `spec.namespace`: Target namespace (defaults to current)
- `spec.labels`: Additional labels for BareMetalHost
- `spec.server_vendor`: Explicit vendor — `HP`, `DELL`, or `CISCO` (case-insensitive)
- `spec.network.vlanId`: VLAN ID integer (1–4094); triggers NMStateConfig creation for Dell servers

### Status Phases

- **Processing**: Querying management systems
- **Buffered**: Server info retrieved, waiting for available slot
- **Completed**: BareMetalHost successfully created
- **Failed**: Error occurred

## Dependencies

Key Python packages (from [requirements.txt](requirements.txt)):
- **kopf** (1.36.1): Kubernetes Operator Pythonic Framework - handles CRD watching and event processing
- **kubernetes** (28.1.0): Official K8s Python client - for creating BareMetalHost resources and secrets
- **ucsmsdk**: Cisco UCS Manager SDK - for querying UCS Manager
- **ucscsdk**: Cisco UCS Central SDK - for querying UCS Central
- **requests**: HTTP library - for REST API calls to HP OneView and Dell OME
- **pyyaml**: YAML parser - for generating BareMetalHost YAML manifests

## Development Commands

### Running Locally

```bash
# Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate  # or `.venv/Scripts/activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Configure using environment variables
# Copy the example file and edit it
cp .env.example .env
# Edit .env with your actual credentials

# Or export variables directly:
export LOG_LEVEL=DEBUG  # Optional: DEBUG, INFO, WARNING, ERROR
export MAX_AVAILABLE_SERVERS=20  # Optional: default is 20
export BUFFER_CHECK_INTERVAL=30  # Optional: default is 30 seconds

# Configure at least one vendor management system:

# HP OneView (choose one set of variables)
export HP_ONEVIEW_IP="10.0.0.1"
export HP_ONEVIEW_PASSWORD="password"
export HP_BMC_USERNAME="Administrator"  # BMC credentials for HP servers
export HP_BMC_PASSWORD="ilo-password"

# Cisco UCS Central
export UCS_CENTRAL_IP="10.0.0.2"
export UCS_CENTRAL_PASSWORD="password"
export UCS_MANAGER_PASSWORD="password"
export CISCO_BMC_USERNAME="admin"  # BMC credentials for Cisco servers
export CISCO_BMC_PASSWORD="cimc-password"

# Dell OME
export DELL_OME_IP="10.0.0.3"
export DELL_OME_PASSWORD="password"
export DELL_BMC_USERNAME="root"  # BMC credentials for Dell servers
export DELL_BMC_PASSWORD="calvin"

# Run operator locally (watches all namespaces)
kopf run --liveness=http://0.0.0.0:8080/healthz src/operator_bmh_gen.py --all-namespaces
```

**See [.env.example](.env.example) for a complete list of configuration options.**

### Building and Deploying

```bash
# Build container image
docker build -t <registry>/bmh-generator-operator:latest .

# Push to registry
docker push <registry>/bmh-generator-operator:latest

# Deploy to Kubernetes
kubectl apply -f deploy/crd.yaml
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/deployment.yaml
```

### Testing in Kubernetes

```bash
# Check operator logs
kubectl logs -n metal3-system -l app=bmh-generator-operator -f

# Create test BareMetalHostGenerator
kubectl apply -f deploy/example.yaml

# Check resource status (shorthand: bmhgen)
kubectl get bmhgen -A
kubectl describe bmhgen <name> -n <namespace>

# Check created BareMetalHost (shorthand: bmh)
kubectl get bmh -A

# Watch buffer activity
kubectl logs -n metal3-system -l app=bmh-generator-operator -f | grep -i buffer

# Check available vs buffered servers
kubectl get bmhgen -A -o json | jq '.items[] | select(.status.phase=="Buffered") | {name: .metadata.name, phase: .status.phase}'
```

### Testing Python Code Locally

```bash
# Compile all Python files to check for syntax errors
python3 -m py_compile src/*.py

# Test configuration module
python3 -c "from src.config import validate_configuration; print(validate_configuration())"

# Test imports (run from project root)
python3 -c "from src.unified_server_client import UnifiedServerClient; print('OK')"
```

## Key Implementation Details

### Buffer Management

- **MAX_AVAILABLE_SERVERS**: 20 (configurable via env var)
- **BUFFER_CHECK_INTERVAL**: 30 seconds (configurable via env var)
- Available BMHs are those NOT in "provisioned" state
- When limit reached, new servers stored in "Buffered" phase with MAC/IP/vendor info
- Background task `buffer_check_loop()` periodically releases buffered servers

### Multi-Vendor Connection Pattern

```python
# The operator creates a new UnifiedServerClient instance for each lookup
# via get_unified_connection() to ensure fresh connections
with get_unified_connection() as client:
    mac_address, ipmi_address = client.get_server_info(server_name, server_vendor)
```

### Credential Management

The operator uses a two-phase credential model via [src/config.py](src/config.py):

**Phase 1: Management System Credentials** (operator connects to query server info):
- HP: `HP_ONEVIEW_IP`, `HP_ONEVIEW_USERNAME`, `HP_ONEVIEW_PASSWORD`
- Cisco: `UCS_CENTRAL_IP`, `UCS_CENTRAL_USERNAME`, `UCS_CENTRAL_PASSWORD`, `UCS_MANAGER_USERNAME`, `UCS_MANAGER_PASSWORD`
- Dell: `DELL_OME_IP`, `DELL_OME_USERNAME`, `DELL_OME_PASSWORD`

**Phase 2: BMC Credentials** (stored in K8s secrets, used by Ironic/Metal3 to provision servers):
- HP iLO: `HP_BMC_USERNAME` (default: Administrator), `HP_BMC_PASSWORD`
- Cisco CIMC: `CISCO_BMC_USERNAME` (default: admin), `CISCO_BMC_PASSWORD`
- Dell iDRAC: `DELL_BMC_USERNAME` (default: root), `DELL_BMC_PASSWORD` (default: calvin)
- Fallback: `DEFAULT_BMC_USERNAME`, `DEFAULT_BMC_PASSWORD`

**Important**: Each vendor can have different BMC credentials. The operator creates vendor-specific secrets (e.g., `hp-cred-server01`, `dell-cred-server02`) with the appropriate credentials.

At startup, the operator validates at least one vendor system is configured. See [src/CONFIG_README.md](src/CONFIG_README.md) for details.

### Kopf Framework

This operator uses Kopf (Kubernetes Operator Pythonic Framework):
- **Handlers**: `@kopf.on.create`, `@kopf.on.update`, `@kopf.on.delete`
- **Lifecycle**: `@kopf.on.startup()`, `@kopf.on.cleanup()`
- **Status Updates**: Use `custom_api.patch_namespaced_custom_object_status()`
- **Finalizers**: Configured as `bmhgenerator.infra.example.com/finalizer`
- **Progress Storage**: Uses annotations via `kopf.AnnotationsProgressStorage()`

### Code Organization Notes

- The codebase has both `UnifiedServerClient` (legacy) and strategy pattern implementations
- The operator currently uses `UnifiedServerClient` in [src/operator_bmh_gen.py](src/operator_bmh_gen.py:21)
- Strategy pattern classes exist but may not be fully integrated yet
- When refactoring, verify which client implementation is actually being used in the operator handlers

### Important Patterns

1. **Async/Await**: The operator uses asyncio extensively - all Kopf handler functions are `async`
2. **Background Event Loop**: Buffer checking runs in a separate thread with its own event loop (see [src/operator_bmh_gen.py](src/operator_bmh_gen.py:85-91))
3. **Lock Usage**: `bmh_buffer_lock` (asyncio.Lock) protects buffer operations to prevent race conditions
4. **Error Handling**: Use `kopf.PermanentError` for unrecoverable errors (server not found, invalid config)
5. **Status Updates**: Always update CR status to reflect current phase (Processing → Buffered/Completed/Failed)
6. **Resource Creation**: Check for 409 (already exists) errors - they're expected when resources already exist

### API Interactions

The operator interacts with:
- **CustomObjectsApi**: For BareMetalHost and BareMetalHostGenerator CRDs
- **CoreV1Api**: For Secret creation
- **HP OneView REST API**: `/rest/login-sessions`, `/rest/server-hardware`
- **Cisco UCS SDK**: ucsmsdk/ucscsdk Python packages
- **Dell OME REST API**: `/api/SessionService/Sessions`, `/api/DeviceService/Devices`

## Common Issues

### Server Not Found
- Verify server name matches exactly (case-insensitive comparison is used)
- Check `spec.server_vendor` is set correctly (HP, DELL, or CISCO)
- Ensure management system credentials are valid
- Check operator logs for connection errors

### Buffering Behavior
- Servers buffer when available BMH count >= 20
- "Available" means `provisioning.state != "provisioned"`
- Buffer releases happen every 30 seconds
- Check buffer logs with: `kubectl logs -l app=bmh-generator-operator | grep -i buffer`

## Common Development Workflows

### Adding Support for a New Vendor

1. Create new strategy class in `src/` implementing `ServerStrategy` interface
2. Add vendor detection logic to `ServerTypeDetector.detect()` in [src/server_strategy.py](src/server_strategy.py:56-86)
3. Register strategy in `ServerStrategyFactory._strategies` dict
4. Add BMC address format to `BMCAddressFormat` in [src/config.py](src/config.py)
5. Add environment variables for management system and BMC credentials to [src/config.py](src/config.py)
6. Update [.env.example](.env.example) with new vendor variables
7. Add vendor to CRD enum in [deploy/crd.yaml](deploy/crd.yaml:45)

### Modifying Buffer Behavior

1. Adjust `MAX_AVAILABLE_SERVERS` or `BUFFER_CHECK_INTERVAL` in [src/config.py](src/config.py) defaults
2. Modify `buffer_check_loop()` in [src/buffer_manager.py](src/buffer_manager.py) for logic changes
3. Test with: `kubectl get bmh -A -o json | jq '[.items[] | select(.status.provisioning.state != "provisioned")] | length'`

### Changing BMC Credentials or Formats

1. Edit `BMCCredentials` class in [src/config.py](src/config.py) for credential retrieval
2. Edit `BMCAddressFormat` class in [src/config.py](src/config.py) for address formatting
3. Changes automatically apply to [src/yaml_generators.py](src/yaml_generators.py) which imports these helpers

### Testing Strategy Pattern Changes

1. Test each strategy independently first
2. Verify `ServerTypeDetector.detect()` works with all naming conventions
3. Ensure `ServerStrategyFactory.create_strategy()` returns correct implementations
4. Check that credentials are properly passed to each strategy
5. Test fallback behavior when one vendor system is unavailable

## Deployment Structure

- **CRD**: `deploy/crd.yaml` - BareMetalHostGenerator custom resource definition
- **RBAC**: `deploy/rbac.yaml` - ServiceAccount, ClusterRole, ClusterRoleBinding
- **Deployment**: `deploy/deployment.yaml` - Operator deployment with env vars
- **Example**: `deploy/example.yaml` - Sample BareMetalHostGenerator resource

## Monitoring and Debugging

The operator uses structured logging with separate loggers:
- `bmh_logger`: BMH generation operations
- `ucs_logger`: UCS client operations
- `operator_logger`: Kubernetes operator lifecycle
- `buffer_logger`: Buffer management

Each log includes: timestamp, logger name, level, function name, line number, message

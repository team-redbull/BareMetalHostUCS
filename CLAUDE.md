# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Kubernetes operator that automatically creates BareMetalHost resources by querying multiple server management systems (HP OneView, Cisco UCS Central, Dell OpenManage Enterprise, and Cisco Intersight). The operator bridges vendor-specific management systems with Metal3/OpenShift bare metal deployments.

## Architecture

### Core Components

1. **Operator Entry Point**: `src/operator_bmh_gen.py`
   - Kopf-based Kubernetes operator that watches BareMetalHostGenerator CRDs
   - Handles resource creation, updates (redeploy annotation), and deletion
   - Implements buffering logic to prevent resource exhaustion
   - Single-worker serial processing (`settings.execution.max_workers = 1`)

2. **Unified Client**: `src/unified_server_client.py`
   - Active client used by the operator — delegates to strategy implementations
   - `initialize_unified_client()` reads env vars and builds strategies for each configured vendor
   - `get_server_info()` tries vendors in priority order (detected or specified type first)

3. **Strategy Pattern**: `src/server_strategy.py` + vendor implementations
   - `src/server_strategy.py`: `ServerStrategy` ABC, `ServerTypeDetector`, `ServerStrategyFactory`
   - `src/hp_server_strategy.py`: HP OneView integration
   - `src/ucs_server_strategy.py`: Cisco UCS Central/Manager integration
   - `src/dell_server_strategy.py`: Dell OME integration
   - `src/intersight_server_strategy.py`: Cisco Intersight integration via the official `intersight` Python SDK (HTTP-signature auth)
   - Each strategy implements: `is_configured()`, `ensure_connected()`, `get_server_info()`, `disconnect()`
   - `get_server_info(server_name, mac_indices)` returns `(list_of_MACs, bmc_ip)`. `mac_indices` (from the server profile or `spec.networkConfig`) selects which NIC MACs to return — two for a bond, one for a single NIC. The shared selector is `select_macs()`/`select_from_ordered()` in `src/server_profile_config.py`.

4. **Buffer Manager**: `src/buffer_manager.py`
   - Controls the number of available BareMetalHosts in the cluster (default: 20)
   - FIFO queue for buffered servers
   - Background `asyncio.Task` (not a thread) created in startup checks every 30 seconds

5. **Server Profile Config**: `src/server_profile_config.py`
   - Maps server name patterns to `nic_names` (list) + `mac_indices` (list) for the bonded NMStateConfig
   - Two entries per list → 802.3ad bond; one entry → single-NIC (non-bonded) config
   - `nic_names`/`mac_indices` are required, parallel lists of equal length
   - `mac_indices` values are `first`/`last`/integer; `select_macs()` resolves them against the ordered NIC MACs returned by each strategy
   - Loaded from ConfigMap at `/config/profiles.yaml` (`deploy/configmap-server-profiles.yaml`)
   - Falls back to built-in defaults (h100, h200, 10tb-, default) if ConfigMap absent
   - Profiles are a singleton — restart operator pod to pick up ConfigMap changes

6. **YAML Generators**: `src/yaml_generators.py`
   - Creates BareMetalHost, BMC Secret, and NMStateConfig resource definitions
   - Contains `get_bmc_credentials()` and `get_bmc_address()` helper functions (not in config.py)
   - `generate_nmstate_config(name, namespace, mac_addresses, nic_names, infra_env, vlanId, bond_name="bond0", bond_mode="802.3ad")` — builds a bonded config (two ethernet members → bond → VLAN-on-bond) when given two NICs, or the single-NIC layout (VLAN directly on the NIC) when given one. `bootMACAddress` on the BMH uses the first MAC.
   - Vendor-specific BMC address formats:
     - HP: `redfish-virtualmedia://{ip}/redfish/v1/Systems/1`
     - Dell: `idrac-virtualmedia://{ip}/redfish/v1/Systems/System.Embedded.1`
     - Cisco: `ipmi://{ip}:623`
     - Intersight: `redfish-virtualmedia://{ip}/redfish/v1/Systems/1` (confirm Systems path against your CIMC)
   - BMC secret naming: `{vendor}-cred-{server_name}` (e.g., `hp-cred-server01`)
   - NMStateConfig naming: `nmstate-config-{name}`

7. **Configuration Module**: `src/config.py`
   - Centralized logging setup, buffer constants, CRD constants (`BMHGenCRD`, `BMHCRD`, `NMStateConfigCRD`), and `Phase` constants
   - Does **not** contain BMC credential or address logic — those are in `yaml_generators.py`

8. **OpenShift Utilities**: `src/openshift_utils.py`
   - Helper functions for creating, updating, and deleting Kubernetes resources

### Vendor Detection Logic

The operator detects server vendor in this priority order:

1. **`spec.server_vendor`**: `HP`, `DELL`, `CISCO`, or `INTERSIGHT` (case-insensitive; validated by CRD schema)
2. **Name-based heuristics** (when `spec.server_vendor` is omitted):
   - Contains `hp` → HP
   - Contains `dell` → Dell
   - Contains `intersight` → Intersight
   - Contains `cisco` → Cisco
   - Default → Cisco

### Custom Resource Definition

**Group**: `infra.example.com` | **Version**: `v1alpha1` | **Kind**: `BareMetalHostGenerator`

**Required Fields**:

- `spec.infraEnv`: InfraEnv name for OpenShift Agent-based installation
- `spec.networkConfig.vlanId`: VLAN ID integer (1–4094). Required for **all** vendors — the handler raises `kopf.PermanentError` (Failed phase) if missing. (Not a hard CRD `required:` so existing stored CRs don't break on update.)

**Optional Fields**:

- `spec.serverName`: Server name in management system (defaults to CR name)
- `spec.namespace`: Target namespace (defaults to current)
- `spec.labels`: Additional labels for BareMetalHost
- `spec.server_vendor`: Explicit vendor — `HP`, `DELL`, `CISCO`, or `INTERSIGHT` (case-insensitive)
- `spec.networkConfig.nicNames`: list of bond-member NIC names (overrides profile lookup; must be set with `macIndices` of equal length)
- `spec.networkConfig.macIndices`: list of MAC selectors (`first`/`last`/integer), parallel to `nicNames`

### Status Phases

- **Processing**: Querying management systems
- **Buffered**: Server info retrieved, waiting for available slot
- **Completed**: BareMetalHost successfully created
- **Failed**: Error occurred

### Redeploy Annotation

Set annotation `redeploy: "true"` on a BareMetalHostGenerator to trigger recreation of its BMH, Secret, and NMStateConfig. Only works for `Completed` or `Failed` phases. The annotation is automatically removed after redeploy (success or failure) to prevent loops.

## Development Commands

### Running Locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit with actual credentials

# Run operator locally (watches all namespaces)
kopf run --liveness=http://0.0.0.0:8080/healthz src/operator_bmh_gen.py --all-namespaces
```

### Building and Deploying

```bash
docker build -t <registry>/bmh-generator-operator:latest .
docker push <registry>/bmh-generator-operator:latest

kubectl apply -f deploy/crd.yaml
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/configmap-server-profiles.yaml
kubectl apply -f deploy/deployment.yaml
```

### Testing in Kubernetes

```bash
kubectl logs -n metal3-system -l app=bmh-generator-operator -f
kubectl apply -f deploy/example.yaml
kubectl get bmhgen -A
kubectl describe bmhgen <name> -n <namespace>
kubectl get bmh -A

# Check buffered servers
kubectl get bmhgen -A -o json | jq '.items[] | select(.status.phase=="Buffered") | {name: .metadata.name, phase: .status.phase}'
```

### Testing Python Code Locally

```bash
# Check for syntax errors
python3 -m py_compile src/*.py

# Test imports
python3 -c "from src.unified_server_client import UnifiedServerClient; print('OK')"
python3 -c "from src.server_profile_config import get_server_profile_config; print('OK')"
```

### Linting (from CI)

```bash
pip install flake8 black isort mypy
flake8 src/ --max-line-length=88 --extend-ignore=E203,W503
black --check src/
isort --check-only src/
mypy src/ --ignore-missing-imports
```

## Key Implementation Details

### Environment Variables

**Management System Credentials** (operator uses to query server info):

- HP OneView: `ONEVIEW_IP`, `ONEVIEW_USERNAME`, `ONEVIEW_PASSWORD`
- Cisco UCS: `UCS_CENTRAL_IP`, `UCS_CENTRAL_USERNAME`, `UCS_CENTRAL_PASSWORD`, `UCS_MANAGER_USERNAME`, `UCS_MANAGER_PASSWORD`
- Dell OME: `OME_IP`, `OME_USERNAME`, `OME_PASSWORD`
- Cisco Intersight: `INTERSIGHT_API_ENDPOINT`, `INTERSIGHT_API_KEY_ID`, `INTERSIGHT_API_SECRET` (PEM key text or path; uses the `intersight` SDK)

**BMC Credentials** (stored in K8s secrets, used by Ironic/Metal3):

- HP iLO: `HP_BMC_USERNAME` (default: Administrator), `HP_BMC_PASSWORD`
- Cisco CIMC: `CISCO_BMC_USERNAME` (default: admin), `CISCO_BMC_PASSWORD`
- Dell iDRAC: `DELL_BMC_USERNAME` (default: root), `DELL_BMC_PASSWORD` (default: calvin)
- Intersight CIMC: `INTERSIGHT_BMC_USERNAME`, `INTERSIGHT_BMC_PASSWORD`

**Operator Behavior**:

- `LOG_LEVEL`: DEBUG, INFO, WARNING, ERROR (default: INFO)
- `MAX_AVAILABLE_SERVERS`: Buffer threshold (default: 20)
- `BUFFER_CHECK_INTERVAL`: Seconds between buffer checks (default: 30)
- `DELETE_RESOURCES_ON_DELETE`: Whether to delete BMH/Secret/NMStateConfig on CR deletion (default: true)
- `SERVER_PROFILES_PATH`: Path to server profiles YAML (default: `/config/profiles.yaml`)

### Async Patterns

All Kopf handler functions are `async`. Blocking calls (K8s API, vendor SDKs) are wrapped in `asyncio.to_thread()`. The buffer check loop runs as an `asyncio.Task` created in `@kopf.on.startup()` — not a separate thread. Use `kopf.PermanentError` for unrecoverable errors (server not found, invalid config). Status updates via `patch.status[...]`.

### Redeploy / Buffer keep two-MAC state in sync

The two-MAC flow threads through three creation paths that must stay consistent: `create_bmh` (immediate), `buffer_manager.process_buffered_generator` (buffer release), and `create_bmh_resources` (redeploy). Buffered CRs persist `macAddresses` + `selectedNicNames`/`selectedMacIndices` in status so release rebuilds the same bond. When changing the MAC/NIC contract, update all three.

### Buffer Management

- Available BMHs are those NOT in "provisioned" state
- When count >= `MAX_AVAILABLE_SERVERS`, new servers are stored in "Buffered" phase
- Background task calls `buffer_manager.buffer_check_iteration()` every `BUFFER_CHECK_INTERVAL` seconds
- `asyncio.Lock` (`bmh_buffer_lock`) in BufferManager protects buffer operations

### Resource Creation Sequence

1. Query management system for the NIC MAC(s) and IPMI/BMC IP (one MAC per `mac_indices` entry)
2. Check buffer — if over limit, store info (incl. `macAddresses`, selected NICs) in CR status and return (Buffered phase)
3. Create `{vendor}-cred-{server}` Secret (BMC credentials)
4. Create BareMetalHost (Metal3 CRD) — `bootMACAddress` = first MAC
5. Create `nmstate-config-{server}` NMStateConfig for **every** vendor — bonded (802.3ad) when two NICs resolve, single-NIC otherwise

## Common Development Workflows

### Adding Support for a New Vendor

(Cisco Intersight in `src/intersight_server_strategy.py` is the most recent worked example.)

1. Create new strategy class in `src/` implementing `ServerStrategy` — `get_server_info()` must return `(list_of_MACs, ip)` and use `select_macs()` for NIC selection
2. Add a `ServerType` enum value + detection pattern in `ServerTypeDetector._NAME_PATTERNS` ([src/server_strategy.py](src/server_strategy.py))
3. Register strategy in `ServerStrategyFactory._init_strategies()` ([src/server_strategy.py](src/server_strategy.py))
4. Add credentials dict entry, `_get_search_order()` entry, and `initialize_unified_client()` env-var reads in [src/unified_server_client.py](src/unified_server_client.py)
5. Add `get_bmc_address()` / `get_bmc_credentials()` / `get_secret_name()` cases in [src/yaml_generators.py](src/yaml_generators.py)
6. Add a strategy logger in [src/config.py](src/config.py)
7. Update [.env.example](.env.example) and add vendor to the CRD `server_vendor` pattern in [deploy/crd.yaml](deploy/crd.yaml)

### Adding a New Server Profile (NIC/MAC mapping)

Edit `deploy/configmap-server-profiles.yaml` (two `nic_names`/`mac_indices` per profile for a bond) and apply it. No image rebuild needed:

```bash
kubectl apply -f deploy/configmap-server-profiles.yaml
kubectl rollout restart deployment/bmh-generator-operator -n metal3-system
```

Alternatively, use `spec.networkConfig.nicNames` + `spec.networkConfig.macIndices` for per-host overrides without changing the ConfigMap.

### Modifying Buffer Behavior

1. Adjust `MAX_AVAILABLE_SERVERS` or `BUFFER_CHECK_INTERVAL` defaults in [src/config.py](src/config.py)
2. Modify `buffer_check_iteration()` in [src/buffer_manager.py](src/buffer_manager.py) for logic changes

## Common Issues

### Server Not Found

- Server name comparison is case-insensitive
- Check `spec.server_vendor` is set correctly (HP, DELL, CISCO, or INTERSIGHT)
- Verify management system credentials (`ONEVIEW_*`, `UCS_*`, `OME_*`, `INTERSIGHT_*`)
- Handler has a 60-second timeout for management system queries
- "Failed to select requested NIC MAC(s)" means the profile/`macIndices` asked for more NICs than the server exposes — check the server's NIC count vs the profile

### Buffering Behavior

- Servers buffer when available BMH count >= `MAX_AVAILABLE_SERVERS`
- "Available" means `provisioning.state != "provisioned"`
- Buffer releases happen every `BUFFER_CHECK_INTERVAL` seconds
- Check buffer logs: `kubectl logs -l app=bmh-generator-operator | grep -i buffer`

## Deployment Structure

- **CRD**: `deploy/crd.yaml`
- **RBAC**: `deploy/rbac.yaml`
- **Deployment**: `deploy/deployment.yaml`
- **Server Profiles ConfigMap**: `deploy/configmap-server-profiles.yaml`
- **Example CR**: `deploy/example.yaml`

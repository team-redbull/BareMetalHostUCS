# BareMetalHost Generator Operator

A Kubernetes operator that automatically creates BareMetalHost resources by querying multiple server management systems (HP OneView, Cisco UCS Central, and Dell OpenManage Enterprise). This operator bridges vendor-specific management systems with Metal3/OpenShift bare metal deployments.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.24%2B-blue.svg)](https://kubernetes.io/)

## Overview

The BareMetalHostGenerator operator:

- ✅ Connects to multiple server management systems (HP OneView, Cisco UCS, Dell OME)
- ✅ Automatically queries server information (MAC addresses, BMC IPs)
- ✅ Creates BareMetalHost resources with vendor-specific BMC configurations
- ✅ Manages buffer to limit available servers (prevents resource exhaustion)
- ✅ Generates BMC secrets with vendor-specific credentials
- ✅ Supports OpenShift Agent-based Installation workflows
- ✅ Handles NMStateConfig for Dell servers with VLAN configuration
- ✅ Per-host NIC and MAC override via `spec.networkConfig`

## Key Features

- **Multi-vendor support**: HP ProLiant (iLO), Cisco UCS (CIMC), Dell PowerEdge (iDRAC)
- **Automatic vendor detection**: Via `spec.server_vendor` or server name patterns
- **Smart buffering**: Limits available BareMetalHosts to 20 (configurable)
- **Dynamic server profiles**: NIC/MAC mapping loaded from a ConfigMap — no image rebuild needed
- **Per-host network override**: `spec.networkConfig` lets you pin `vlanId`, `nicName`, and `macIndex` per CR
- **Vendor-specific BMC protocols**:
  - HP: `redfish-virtualmedia://`
  - Dell: `idrac-virtualmedia://`
  - Cisco: `ipmi://`
- **Thread-safe buffer management**: Background thread for periodic buffer checks
- **Flexible credentials**: Separate credentials per vendor (management system + BMC)

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│ BareMetalHostGenerator CRD                                   │
│ (User creates one per server)                                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ Kopf Operator (Main Thread)                                  │
│ - Watches BMHGen resources                                   │
│ - Queries vendor management systems                          │
│ - Creates BMH resources                                      │
└────────────┬────────────────────────────┬───────────────────┘
             │                            │
             ▼                            ▼
┌────────────────────────┐    ┌──────────────────────────────┐
│ UnifiedServerClient     │    │ Buffer Manager               │
│ - HP OneView           │    │ - Limits available BMHs      │
│ - Cisco UCS Central    │    │ - FIFO queue                 │
│ - Dell OME             │    │ - Background thread (30s)    │
└────────────────────────┘    └──────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│ Generated Resources                                           │
│ - BareMetalHost (Metal3)                                     │
│ - Secret (BMC credentials)                                   │
│ - NMStateConfig (Dell servers with vlanId)                   │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Kubernetes 1.24+ or OpenShift 4.12+
- Metal3 operator installed
- At least one vendor management system configured:
  - HP OneView 8.x+
  - Cisco UCS Central 2.0+
  - Dell OpenManage Enterprise 3.x+
- Python 3.9+ (for development)

## Quick Start

### Installation via Helm (Recommended)

```bash
# Install with HP OneView
helm install bmh-generator deploy/helm/bmh-generator-operator \
  --namespace metal3-system \
  --create-namespace \
  --set hpOneView.enabled=true \
  --set hpOneView.ip="10.0.0.1" \
  --set hpOneView.password="your-password" \
  --set hpOneView.bmc.password="ilo-password"

# Or install with all vendors
helm install bmh-generator deploy/helm/bmh-generator-operator \
  --namespace metal3-system \
  --create-namespace \
  --values my-values.yaml
```

### Manual Installation

1. **Deploy CRD:**

   ```bash
   kubectl apply -f deploy/crd.yaml
   ```

1. **Create namespace:**

   ```bash
   kubectl create namespace metal3-system
   ```

1. **Deploy RBAC:**

   ```bash
   kubectl apply -f deploy/rbac.yaml
   ```

1. **Create credentials secret:**

   ```bash
   kubectl create secret generic bmh-operator-credentials \
     --namespace=metal3-system \
     --from-literal=ONEVIEW_PASSWORD='your-oneview-password' \
     --from-literal=HP_BMC_PASSWORD='your-ilo-password'
   ```

1. **Deploy operator:**

   ```bash
   kubectl apply -f deploy/deployment.yaml
   ```

1. **Verify:**

   ```bash
   kubectl get pods -n metal3-system
   kubectl logs -n metal3-system -l app=bmh-generator-operator
   ```

## Configuration

### Environment Variables

The operator uses environment variables for configuration. See [.env.example](.env.example) for all options.

#### Core Configuration

```bash
LOG_LEVEL=INFO                    # Logging level
MAX_AVAILABLE_SERVERS=20          # Buffer limit
BUFFER_CHECK_INTERVAL=30          # Check interval in seconds
```

#### Server Profiles Path

```bash
SERVER_PROFILES_PATH=/config/profiles.yaml   # YAML file mounted from ConfigMap
```

Override for local development (see [Dynamic Server Profiles](#dynamic-server-profiles) below).

#### HP OneView (Management System)

```bash
ONEVIEW_IP=10.0.0.1
ONEVIEW_USERNAME=administrator
ONEVIEW_PASSWORD=<secret>

# BMC Credentials (for iLO)
HP_BMC_USERNAME=Administrator
HP_BMC_PASSWORD=<secret>
```

#### Cisco UCS (Management System)

```bash
UCS_CENTRAL_IP=10.0.0.2
UCS_CENTRAL_USERNAME=admin
UCS_CENTRAL_PASSWORD=<secret>
UCS_MANAGER_USERNAME=admin
UCS_MANAGER_PASSWORD=<secret>

# BMC Credentials (for CIMC)
CISCO_BMC_USERNAME=admin
CISCO_BMC_PASSWORD=<secret>
```

#### Dell OME (Management System)

```bash
OME_IP=10.0.0.3
OME_USERNAME=admin
OME_PASSWORD=<secret>

# BMC Credentials (for iDRAC)
DELL_BMC_USERNAME=root
DELL_BMC_PASSWORD=calvin
```

**Important:** Management system credentials are used by the operator to query server info. BMC credentials are used by Metal3/Ironic to provision servers.

#### MongoDB Integration (optional)

```bash
# Leave empty to use vendor APIs directly (default behaviour)
MONGO_URI=mongodb://user:pass@mongo-host:27017/
MONGO_DB_NAME=server_scanner   # default
```

When `MONGO_URI` is set, the operator reads `mac_address` and `bmc_address` from the
`server_scanner.servers` MongoDB collection (populated by the **Scan_Servers** CronJob)
instead of querying vendor APIs on every reconciliation.

**Installed server guard:** if the MongoDB document has `installed: true`, the operator
sets the BMHGen to `Failed` with a message indicating which cluster/MCE already owns the
server, and skips BMH creation entirely. This prevents double-provisioning and saves scan
time.

**Fallback:** if `MONGO_URI` is empty, or the server is not found in MongoDB, or the
document is missing `mac_address`/`bmc_address`, the operator falls back to the vendor
API (original behaviour — fully backward compatible).

## Usage

### Create a BareMetalHostGenerator

```yaml
apiVersion: infra.example.com/v1alpha1
kind: BareMetalHostGenerator
metadata:
  name: worker-01
  namespace: default
spec:
  serverName: "ESXi-Host-01"  # Name in management system
  namespace: "default"         # Target namespace for BMH
  infraEnv: "my-cluster"      # InfraEnv for OpenShift
  server_vendor: HP            # HP, DELL, or CISCO (case-insensitive); omit to auto-detect
  labels:
    node-role.kubernetes.io/worker: ""
  # networkConfig is optional for HP/Cisco; vlanId is required for Dell
  # networkConfig:
  #   vlanId: 100              # Required for Dell (1-4094); triggers NMStateConfig creation
  #   nicName: "ens5f0np0"    # Optional override — must be paired with macIndex
  #   macIndex: "3"            # Optional override — "first", "last", or 0-based integer
```

### `spec.networkConfig`

All network settings live under one `spec.networkConfig` object:

| Field | Required | Description |
| --- | --- | --- |
| `vlanId` | Dell only | VLAN ID (1–4094). Triggers NMStateConfig creation for Dell servers. |
| `nicName` | Optional pair | Exact interface name used in NMStateConfig. Must be set with `macIndex`. |
| `macIndex` | Optional pair | MAC selection — `"first"`, `"last"`, or 0-based integer (e.g. `"3"`). Must be set with `nicName`. |

```yaml
# Dell server — vlanId only (uses default NIC/MAC from server profile)
spec:
  server_vendor: DELL
  networkConfig:
    vlanId: 24

# Dell server — vlanId + explicit NIC/MAC override
spec:
  server_vendor: DELL
  networkConfig:
    vlanId: 24
    nicName: "ens5f0np0"   # overrides profile lookup
    macIndex: "3"           # 0-based index into Dell OME interface list
```

**Rules:**

- `vlanId` is independent — you can set it without `nicName`/`macIndex`.
- `nicName` and `macIndex` must be provided together, or neither. Providing only one raises a permanent error.
- Without `nicName`/`macIndex` the operator falls back to the server profile (see [Dynamic Server Profiles](#dynamic-server-profiles)).
- `macIndex` only applies to Dell MAC selection; HP and Cisco use their own MAC discovery.

After a successful reconciliation, the CR status always shows which NIC and MAC index were used (`selectedNicName`, `selectedMacIndex`), whether they came from the spec override or the profile.

### Apply and Monitor

```bash
# Create the resource
kubectl apply -f worker-01.yaml

# Check status
kubectl get bmhgen -A

# View details
kubectl describe bmhgen worker-01

# Check if BMH was created
kubectl get bmh -A
```

### Status Phases

- **Processing**: Querying management systems
- **Buffered**: Server info retrieved, waiting for available slot
- **Completed**: BareMetalHost created successfully
- **Failed**: Error occurred

### Vendor Detection

The operator detects vendor in this order:

1. **`spec.server_vendor`** (recommended): `HP`, `DELL`, or `CISCO` — case-insensitive, validated by the CRD schema.

1. **Name-based heuristics** (when `spec.server_vendor` is omitted):
   - Contains `hp` → HP
   - Contains `dell` → Dell
   - Contains `cisco` → Cisco
   - Default → Cisco

## Buffer Management

The operator limits available (non-provisioned) BareMetalHosts:

- **Default limit**: 20 servers
- **Check interval**: 30 seconds
- **Behavior**: New servers are buffered when limit reached
- **Release**: FIFO — first buffered, first released

```bash
# Check buffer status
kubectl get bmhgen -A -o json | jq '.items[] | select(.status.phase=="Buffered")'

# View available count
kubectl get bmh -A -o json | jq '[.items[] | select(.status.provisioning.state != "provisioned")] | length'
```

## Dynamic Server Profiles

Server type → NIC/MAC-index mapping is stored in a ConfigMap-mounted YAML file. No image rebuild is needed to add a new server type — update the ConfigMap and roll the deployment.

### Profile Format

```yaml
profiles:
  - pattern: "h100"        # matched case-insensitively against server name
    nic_name: "ens8f0np0"
    mac_index: "2"         # 0-based integer index
  - pattern: "h200"
    nic_name: "ens33f0np0"
    mac_index: "2"
  - pattern: "10tb-"       # trailing dash prevents matching serial numbers like A10TBX123
    nic_name: "ens2f0np0"
    mac_index: "last"      # last NIC/port/partition
  - default: true          # fallback when no pattern matches
    nic_name: "eno12399np0"
    mac_index: "first"     # first NIC/port/partition
```

**`mac_index` values:**

- `"first"` — first interface / first port / first partition
- `"last"` — last interface / last port / last partition
- `"2"` (integer string) — zero-based index into the interface list

Pattern matching is case-insensitive substring search. First match wins.

### Adding a New Server Type

**Via Helm (recommended):**

```yaml
# values.yaml
serverProfiles:
  profiles:
    - pattern: "b200"
      nic_name: "ens4f0np0"
      mac_index: "0"
    # ... existing entries ...
```

```bash
helm upgrade bmh-generator deploy/helm/bmh-generator-operator -n metal3-system
```

**Via standalone ConfigMap:**

```bash
# Edit deploy/configmap-server-profiles.yaml, append entry, then:
kubectl apply -f deploy/configmap-server-profiles.yaml
kubectl rollout restart deployment/bmh-generator-operator -n metal3-system
```

### Local Development Override

```bash
# Point to a local profiles file instead of the ConfigMap mount
export SERVER_PROFILES_PATH=./deploy/configmap-server-profiles.yaml
```

If the file is absent, the operator falls back to built-in defaults (same profiles as shipped in the ConfigMap).

## Helm Chart

### values.yaml Example

```yaml
image:
  repository: quay.io/your-org/bmh-generator-operator
  tag: "1.0.0"

operator:
  logLevel: INFO
  maxAvailableServers: 20
  bufferCheckInterval: 30

hpOneView:
  enabled: true
  ip: "10.0.0.1"
  username: "administrator"
  password: "password"
  bmc:
    username: "Administrator"
    password: "ilo-password"

# Use existing secret (recommended for production)
existingSecret:
  enabled: true
  name: "my-credentials"
```

### Install/Upgrade

```bash
# Install
helm install bmh-generator deploy/helm/bmh-generator-operator -n metal3-system

# Upgrade
helm upgrade bmh-generator deploy/helm/bmh-generator-operator -n metal3-system

# Uninstall
helm uninstall bmh-generator -n metal3-system
```

## Troubleshooting

### Check Logs

```bash
# Operator logs
kubectl logs -n metal3-system -l app.kubernetes.io/name=bmh-generator-operator -f

# Buffer logs
kubectl logs -n metal3-system -l app.kubernetes.io/name=bmh-generator-operator | grep -i buffer

# Connection issues
kubectl logs -n metal3-system -l app.kubernetes.io/name=bmh-generator-operator | grep -i error
```

### Common Issues

#### 1. "No valid configuration found"

- Ensure at least one vendor is configured with BMC credentials
- Check: `kubectl logs ... | grep "Configured systems"`

#### 2. "Server not found"

- Verify server name matches exactly in the management system
- Check `spec.server_vendor` is set correctly (HP, DELL, or CISCO)
- Review search logs: `kubectl logs ... | grep "Searching"`

#### 3. "Buffered instead of created"

- Check available count: `kubectl get bmh -A -o json | jq '[.items[] | select(.status.provisioning.state != "provisioned")] | length'`
- Wait for buffer check (30s interval)
- Or increase `MAX_AVAILABLE_SERVERS`

#### 4. "spec.networkConfig requires both nicName and macIndex"

- You provided only one of the two. Either set both or remove both.

#### 5. Compilation/Import errors

- Check Python version is 3.9+
- Run: `python3 -m py_compile src/*.py`

## Development

### Local Development

```bash
# Clone repository
git clone git@github.com:team-redbull/BareMetalHostUCS.git
cd BareMetalHostUCS

# Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your credentials

# Run locally
kopf run --liveness=http://0.0.0.0:8080/healthz src/operator_bmh_gen.py --all-namespaces
```

### Build Container

```bash
# Build (podman recommended for cross-arch builds)
podman build --platform linux/amd64 -t bmh-generator-operator:dev .
podman push bmh-generator-operator:dev

# Or with docker
docker build -t bmh-generator-operator:dev .
```

### Testing

```bash
# Compile all Python files
python3 -m py_compile src/*.py

# Test configuration
python3 -c "from src.config import validate_configuration; print(validate_configuration())"

# Test imports
python3 -c "from src.unified_server_client import UnifiedServerClient; print('OK')"
```

## Architecture Details

### Threading Model

- **Main Thread**: Kopf event loop handles CRD events
- **Background Thread**: Buffer check runs every 30 seconds
- **Synchronization**: `threading.Lock` protects buffer operations (thread-safe across event loops)

### Credential Model

Two separate credential sets:

1. **Management System**: Used by operator to query servers
   - `ONEVIEW_USERNAME` / `ONEVIEW_PASSWORD`
   - `UCS_CENTRAL_USERNAME` / `UCS_CENTRAL_PASSWORD`
   - `OME_USERNAME` / `OME_PASSWORD`

1. **BMC**: Used by Metal3/Ironic to provision servers
   - `HP_BMC_USERNAME` / `HP_BMC_PASSWORD`
   - `CISCO_BMC_USERNAME` / `CISCO_BMC_PASSWORD`
   - `DELL_BMC_USERNAME` / `DELL_BMC_PASSWORD`

### Strategy Pattern

Each vendor implements `ServerStrategy`:

- `HPServerStrategy` — HP OneView integration
- `CiscoServerStrategy` — UCS Central/Manager integration
- `DellServerStrategy` — Dell OME integration

## Security

- ✅ Store credentials in Kubernetes Secrets
- ✅ Use separate credentials per vendor
- ✅ Base64 encode all secret data
- ✅ Enable RBAC
- ✅ Run with least privilege ServiceAccount
- ✅ Regularly rotate credentials

## Contributing

1. Fork the repository
1. Create a feature branch (`git checkout -b feature/amazing-feature`)
1. Commit your changes (`git commit -m 'Add amazing feature'`)
1. Push to the branch (`git push origin feature/amazing-feature`)
1. Open a Pull Request

## License

Apache License 2.0 - see [LICENSE](LICENSE) file for details.

## Support

- **Documentation**: See [CLAUDE.md](CLAUDE.md) for development guide
- **Issues**: [GitHub Issues](https://github.com/team-redbull/BareMetalHostUCS/issues)
- **Logs**: Check operator logs for detailed error messages

---

**Maintained by**: Roi Blum
**Team**: Red Bull Technology
**Repository**: [github.com/team-redbull/BareMetalHostUCS](https://github.com/team-redbull/BareMetalHostUCS)

---

## Credits

Designed, implemented, and maintained by **Roi Blum** as part of the Red Bull Technology infrastructure team.

![Credits](credits.jpeg)

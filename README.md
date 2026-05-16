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

## Key Features

- **Multi-vendor support**: HP ProLiant (iLO), Cisco UCS (CIMC), Dell PowerEdge (iDRAC)
- **Automatic vendor detection**: Via `spec.server_vendor` or naming patterns
- **Smart buffering**: Limits available BareMetalHosts to 20 (configurable)
- **Vendor-specific BMC protocols**:
  - HP: `redfish-virtualmedia://`
  - Dell: `idrac-virtualmedia://`
  - Cisco: `ipmi://`
- **Thread-safe buffer management**: Background thread for periodic buffer checks
- **Flexible credentials**: Separate credentials per vendor (management system + BMC)

## Architecture

```
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
│ - NMStateConfig (Dell servers with VLAN)                     │
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
# Add Helm repository (if published)
# helm repo add bmh-generator https://your-registry/charts
# helm repo update

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

2. **Create namespace:**
```bash
kubectl create namespace metal3-system
```

3. **Deploy RBAC:**
```bash
kubectl apply -f deploy/rbac.yaml
```

4. **Create credentials secret:**
```bash
kubectl create secret generic bmh-operator-credentials \
  --namespace=metal3-system \
  --from-literal=ONEVIEW_PASSWORD='your-oneview-password' \
  --from-literal=HP_BMC_PASSWORD='your-ilo-password'
```

5. **Deploy operator:**
```bash
# Update deploy/deployment.yaml with your registry and credentials
kubectl apply -f deploy/deployment.yaml
```

6. **Verify:**
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

## Usage

### Create a BareMetalHostGenerator

```yaml
apiVersion: infra.example.com/v1alpha1
kind: BareMetalHostGenerator
metadata:
  name: worker-01
  namespace: default
spec:
  serverName: "ESXi-Host-01" # Name in management system
  namespace: "default"        # Target namespace for BMH
  infraEnv: "my-cluster"     # InfraEnv for OpenShift
  server_vendor: HP           # HP, DELL, or CISCO (case-insensitive); omit to auto-detect
  network:
    vlanId: 100               # Optional: VLAN ID (1-4094) for Dell NMStateConfig
  labels:
    node-role.kubernetes.io/worker: ""
  # Optional: override NIC and MAC selection for this specific host
  # networkConfig:
  #   nicName: "ens5f0np0"   # both fields required together
  #   macIndex: "3"
```

### Per-host NIC and MAC Override (`spec.networkConfig`)

By default the operator selects the NIC name and MAC address index from the server
profile matching the server name (see [Dynamic Server Profiles](#dynamic-server-profiles)).
When a host is non-standard or you know exactly which NIC/MAC to use, you can override
profile lookup directly in the CR spec:

```yaml
spec:
  infraEnv: my-cluster
  networkConfig:
    nicName: "ens5f0np0"   # exact interface name — used in NMStateConfig
    macIndex: "3"           # 0-based index into the Dell OME interface list
                            # also accepts "first" or "last"
```

**Rules:**
- Both `nicName` and `macIndex` must be provided together, or neither. Providing only one raises a permanent error.
- The override applies to **Dell** servers for MAC selection (`macIndex`) and to all vendors for NMStateConfig NIC naming (`nicName`).
- Without `networkConfig` the behavior is identical to before — fully backward compatible.

`macIndex` accepted values:

| Value | Selects |
|---|---|
| `"first"` | first interface / first port / first partition |
| `"last"` | last interface / last port / last partition |
| `"2"` (integer string) | zero-based index (e.g. `"2"` → third interface) |

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
- **Buffered**: Server info retrieved, waiting for slot
- **Completed**: BareMetalHost created successfully
- **Failed**: Error occurred

### Vendor Detection

The operator detects vendor in this order:

1. **`spec.server_vendor`** (recommended): `HP`, `DELL`, or `CISCO` — case-insensitive, validated by the CRD schema.

2. **Name-based heuristics** (when `spec.server_vendor` is omitted):
   - Contains `hp` → HP
   - Contains `dell` → Dell
   - Contains `cisco` → Cisco
   - Default → Cisco

## Buffer Management

The operator limits available (non-provisioned) BareMetalHosts:

- **Default limit**: 20 servers
- **Check interval**: 30 seconds
- **Behavior**: New servers are buffered when limit reached
- **Release**: FIFO - first buffered, first released

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
  - pattern: "10tb-"
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

### Local Development

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

**1. "No valid configuration found"**
- Ensure at least one vendor is configured with BMC credentials
- Check: `kubectl logs ... | grep "Configured systems"`

**2. "Server not found"**
- Verify server name matches exactly
- Check vendor annotation is correct
- Review search logs: `kubectl logs ... | grep "Searching"`

**3. "Buffered instead of created"**
- Check available count: `kubectl get bmh -A -o json | jq '...'`
- Wait for buffer check (30s interval)
- Or increase `MAX_AVAILABLE_SERVERS`

**4. Compilation/Import errors**
- Operator now uses lazy imports to avoid circular dependencies
- Check Python version is 3.9+

## Development

### Local Development

```bash
# Clone repository
git clone https://github.com/roiblum1/BareMetalHostUCS.git
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
# Build
docker build -t bmh-generator-operator:dev .

# Or with podman
podman build -t bmh-generator-operator:dev .

# Test in minikube
minikube image load bmh-generator-operator:dev
kubectl run test --image=bmh-generator-operator:dev --image-pull-policy=Never
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

2. **BMC**: Used by Metal3/Ironic to provision servers
   - `HP_BMC_USERNAME` / `HP_BMC_PASSWORD`
   - `CISCO_BMC_USERNAME` / `CISCO_BMC_PASSWORD`
   - `DELL_BMC_USERNAME` / `DELL_BMC_PASSWORD`

### Strategy Pattern

Each vendor implements `ServerStrategy`:
- `HPServerStrategy` - HP OneView integration
- `CiscoServerStrategy` - UCS Central/Manager integration
- `DellServerStrategy` - Dell OME integration

## Security

- ✅ Store credentials in Kubernetes Secrets
- ✅ Use separate credentials per vendor
- ✅ Base64 encode all secret data
- ✅ Enable RBAC
- ✅ Run with least privilege ServiceAccount
- ✅ Regularly rotate credentials

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

Apache License 2.0 - see [LICENSE](LICENSE) file for details.

## Support

- **Documentation**: See [CLAUDE.md](CLAUDE.md) for development guide
- **Issues**: [GitHub Issues](https://github.com/roiblum1/BareMetalHostUCS/issues)
- **Logs**: Check operator logs for detailed error messages

---

**Maintained by**: Roi Blum  
**Team**: Red Bull Technology  
**Repository**: https://github.com/team-redbull/BareMetalHostUCS

---

## Credits

Designed, implemented, and maintained by **Roi Blum** as part of the Red Bull Technology infrastructure team.

![Credits](credits.jpeg)

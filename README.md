# Palette VMO / VMA Pre-flight Checker

`vma-preflight.py` is a single-file Python tool that validates whether an
environment is ready for the SpectroCloud Palette **Virtual Machine Migration
Assistant (VMA)** to migrate VMs from VMware vSphere to Virtual Machine
Orchestrator (VMO / KubeVirt).

## What it checks

| # | Check | Details |
|---|---|---|
| A | vCenter reachability + auth | Connects to the vCenter SDK endpoint and validates the supplied credentials. Distinguishes DNS, TCP, TLS, and auth failures. |
| B | vCenter privileges required by VMA | Uses `AuthorizationManager.HasPrivilegeOnEntity` to check the connected user against the exact list from the [VMA docs](https://docs.spectrocloud.com/vm-management/vm-migration-assistant/create-source-providers/#prerequisites): every **Virtual Machine > Interaction** privilege, plus **Snapshot management > Create snapshot** and **Remove Snapshot**. Any missing privilege is printed by both API id and UI name. |
| C | ESXi host discovery | Discovers which ESXi hosts back the VMs you plan to migrate (from `vm.runtime.host`) so downstream checks target the right hosts. |
| D | DNS resolution | Resolves the vCenter FQDN and each ESXi host FQDN. Run inside the VMA pod to prove pod-network DNS works. |
| E | TCP port reachability | Opens **TCP 443** to vCenter and **TCP 902** to each ESXi host (the NFC/NBD data path used during migration). |

Exit code is `0` if every check passes, `1` if any FAIL.

## Install

```bash
pip install -r requirements.txt         # installs pyvmomi
```

Only `pyvmomi` is required; DNS and port checks use the Python stdlib.

## Run from an operator laptop or jump host

```bash
python vma-preflight.py \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm      web-01 --vm db-02 \
    --insecure
```

The tool will prompt for the password, or you can pass `--password` or set
`VC_PASSWORD` in the environment.

## Run from inside the VMA pod (recommended for the real DNS + network view)

```bash
NS=<vma-namespace>
POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=vm-migration-assistant \
        -o jsonpath='{.items[0].metadata.name}')

kubectl -n "$NS" cp vma-preflight.py "$POD:/tmp/vma-preflight.py"
kubectl -n "$NS" exec -it "$POD" -- \
    python3 /tmp/vma-preflight.py \
        --vcenter vcenter.corp.example.com \
        --user    svc-vma@vsphere.local \
        --vm      web-01 --vm db-02 \
        --insecure
```

If `pyvmomi` is not present in the pod, run the two connectivity checks alone
and do the vCenter check from the laptop:

```bash
# Inside the pod: DNS + TCP only, no pyvmomi needed
kubectl -n "$NS" exec -it "$POD" -- \
    python3 /tmp/vma-preflight.py \
        --vcenter vcenter.corp.example.com \
        --esxi    esxi01.corp.example.com \
        --esxi    esxi02.corp.example.com \
        --skip-vcenter
```

## Useful flags

| Flag | Purpose |
|---|---|
| `--vm NAME` (repeatable) | Check permissions on each named VM and discover just their ESXi hosts. Without it, permissions are checked on the vCenter root folder. |
| `--esxi HOST` (repeatable) | Add extra ESXi hosts to DNS + port 902 checks (useful when discovery is skipped). |
| `--insecure` | Skip TLS verification for self-signed vCenter certs. |
| `--skip-vcenter` / `--skip-dns` / `--skip-ports` | Run a subset. |
| `--timeout 5` | Per-connection timeout (seconds). |

## What "missing privilege" looks like

```
== B. vCenter privileges required by VMA ==
  [PASS] Authenticated principal  — svc-vma@vsphere.local
  [FAIL] Missing privileges on vCenter root folder  — 3 of 28 missing
      - VirtualMachine.State.CreateSnapshot  (Snapshot management > Create snapshot)
      - VirtualMachine.State.RemoveSnapshot  (Snapshot management > Remove Snapshot)
      - VirtualMachine.Interact.PowerOff     (Interaction > Power Off)
```

Copy the API id or the UI name into the vCenter Role editor to add the missing
privilege, then re-run the tool.

## Source of truth for the permission list

The required-privilege set is baked in at the top of `vma-preflight.py` in the
`VMA_REQUIRED_PRIVILEGES` dict. It mirrors the SpectroCloud VMA "Create Source
Providers" prerequisites (all *Virtual Machine > Interaction* privileges + the
two Snapshot management privileges) with the API ids taken from Broadcom's
vSphere 8.0 "Defined Privileges" reference. If SpectroCloud updates the list,
edit that dict.

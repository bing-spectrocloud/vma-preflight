# Palette VMO / VMA Pre-flight Checker

`vma-preflight.py` is a single-file Python tool that validates whether an
environment is ready for the SpectroCloud Palette **Virtual Machine Migration
Assistant (VMA)** to migrate VMs from VMware vSphere to Virtual Machine
Orchestrator (VMO / KubeVirt).

It runs three groups of checks, chosen from an interactive menu (no flags)
or explicitly via `--run`.

## Three run modes

| `--run` | Section | What it does |
|---|---|---|
| `preflight` | A / B / C / D / E | vCenter auth + all VMA-required vCenter privileges, ESXi discovery, DNS resolution, TCP 443 to vCenter and 902 to ESXi. Read-only. |
| `allvms` | F / G | Per-VM vSphere-side pre-migration checks: vCenter version for virt-v2v, guest OS supportability, VMware Tools, snapshots, Secure Boot + PXE boot order, CD-ROM / disk / RDM / IDE controller, NIC port group + VLAN mode (incl. trunk / PVLAN / MAC learning). Read-only. |
| `windows` | H | Per-Windows-VM in-guest checks + optional pre-migration prep via WinRM: Basic vs Dynamic disks, `Confirm-SecureBootUEFI`, hibernation state, Fast Startup state, clean guest shutdown, vSphere power-state verification. Changes are **dry-run by default**; pass `--apply` to make them. |
| `all` | A–H | preflight + allvms + windows. |

Exit code is `0` if every check passes, `1` if any FAIL.

## Install

```bash
pip install -r requirements.txt
```

- `pyvmomi` is always required.
- `pywinrm` is required only for `--run windows` (or `--run all` against Windows VMs).

## Interactive menu (no flags)

```bash
python vma-preflight.py
```

Prints:

```
Palette VMO / VMA Pre-flight - what do you want to run?

  1) vCenter environment preflight only
  2) All-VMs vSphere-side checks only
  3) Windows VM pre-migration checks
  4) All of the above
  q) Quit
```

Flags supplied on the same command line still apply.

## Common invocations

**Existing vCenter preflight** (unchanged from v1):

```bash
python vma-preflight.py --run preflight \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm web-01 --vm db-02 \
    --insecure
```

**All-VMs vSphere-side pre-migration checks** for a specific wave of VMs:

```bash
python vma-preflight.py --run allvms \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm web-01 --vm db-02 --vm win-01 \
    --insecure
```

**Windows in-guest checks, dry-run first**:

```bash
python vma-preflight.py --run windows \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm      win-01 \
    --win-user Administrator \
    --insecure
```

Dry-run output shows the pending changes as `[PLAN]` lines. Nothing is written
to the guest.

**Windows in-guest checks with changes applied** (hibernation off, clean shutdown):

```bash
python vma-preflight.py --run windows --apply \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm      win-01 \
    --win-user Administrator \
    --insecure
```

Applied changes show as `[DONE]`. After `shutdown /s /t 0`, the script polls
vSphere for up to 120s to confirm the VM reaches `poweredOff`.

**Everything, end to end**:

```bash
python vma-preflight.py --run all --apply \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm      web-01 --vm win-01 \
    --win-user Administrator \
    --insecure
```

**Long VM lists — use `--vm-file`**:

```bash
python vma-preflight.py --run allvms \
    --vcenter vcenter.corp.example.com --user svc-vma@vsphere.local \
    --vm-file wave1.txt \
    --insecure
```

Where `wave1.txt` looks like:

```
# Wave 1 - production frontend
web-01
web-02   # HA pair
db-01

# Wave 1 - windows fileservers
win-file-01
win-file-02
```

Blank lines and `#` comments are skipped; trailing `# ...` on a line is
stripped. `--vm-file` merges with any `--vm` flags on the same command
(duplicates removed, first occurrence wins). The startup line prints how many
VMs came from each source, e.g.:

```
VM list: 6 unique VM(s) (1 from --vm, 5 added from wave1.txt)
```

## Flag reference

### vCenter

| Flag | Purpose |
|---|---|
| `--vcenter HOST` | vCenter endpoint (`host`, `host:port`, `https://host/sdk`). |
| `--user USER` | vCenter username. |
| `--password` \| `VC_PASSWORD` | Password. Prompts securely if neither is set. |
| `--insecure` | Skip TLS verification for self-signed vCenter certs. |
| `--vm NAME` | VM to include (repeatable). Needed for per-VM permission, all-VMs, and Windows checks. |
| `--vm-file PATH` | Text file with one VM name per line. Blank lines and lines starting with `#` are ignored; trailing `# comments` are stripped. Merges with `--vm` (duplicates removed). |
| `--esxi HOST` | Extra ESXi host for DNS + TCP 902 (repeatable). |
| `--skip-dns` / `--skip-ports` | Trim the preflight to a subset. |
| `--timeout N` | Per-connection timeout in seconds (default 5). |

### Windows guest (WinRM)

| Flag | Purpose |
|---|---|
| `--win-user USER` | Windows guest username (e.g. `Administrator` or `DOMAIN\\svc`). |
| `--win-password` \| `WIN_PASSWORD` | Guest password. Prompts securely if neither is set. |
| `--win-auth {ntlm,basic,kerberos,credssp}` | WinRM auth mechanism (default `ntlm`). |
| `--win-transport {http,https}` | WinRM transport (default `http`, port 5985). |
| `--win-port N` | WinRM port override (defaults: 5985 http / 5986 https). |
| `--apply` | Actually make the Windows changes. Without it, changes are dry-run and appear as `[PLAN]`. |

## What "changes" the script can make on Windows

Only these, and only when `--apply` is passed. Every other Windows check is
read-only.

1. **Hibernation off** — `powercfg /h off`. Deletes `hiberfil.sys`; also
   disables Fast Startup as a side effect.
2. **Clean guest shutdown** — `shutdown /s /t 0` inside the guest, then
   polling `vm.runtime.powerState` in vSphere for up to 120 seconds.

No files, services, drivers, or registry keys other than those touched by
those two commands are modified.

## WinRM prerequisites on the target Windows guest

- WinRM service running: `winrm quickconfig`
- HTTP listener on 5985 (or HTTPS on 5986) reachable from wherever the script
  runs
- Firewall rule: `New-NetFirewallRule -Name "WinRM-HTTP" -DisplayName "WinRM HTTP" -Enabled True -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow`
- For NTLM auth from a non-domain host, enable it on the guest:
  `Enable-PSRemoting -Force` and `Set-Item WSMan:\localhost\Service\Auth\Basic $true` (only if you plan to use `--win-auth basic`)

## Sample output snippets

### VMA-required vCenter privileges missing

```
== B. vCenter privileges required by VMA ==
  [PASS] Authenticated principal  — svc-vma@vsphere.local
  [FAIL] Missing privileges on VM 'win-01'  — 23 of 28 missing
      - VirtualMachine.Interact.Reset      (Interaction > Reset)
      - VirtualMachine.Interact.Suspend    (Interaction > Suspend)
      ...
```

### All-VMs checks

```
== G. Per-VM vSphere-side pre-migration checks ==

  VM: win-01
  [PASS] [win-01] Guest OS                        — windows2019srv_64Guest ... supported by virt-v2v
  [PASS] [win-01] VMware Tools                    — running, version 12352
  [PASS] [win-01] Snapshots                       — no active snapshots
  [FAIL] [win-01] Secure Boot                     — EFI Secure Boot is ENABLED. Either disable in vSphere ...
  [PASS] [win-01] Boot order                      — no PXE / network device in boot order
  [PASS] [win-01] CD-ROM / removable media        — no ISO or host CD-ROM attached
  [PASS] [win-01] Disks                           — 2 SCSI/paravirtual disk(s)
  [PASS] [win-01] NICs                            — 1 NIC(s)
        • Vmxnet3 @ 'app-net' (DVS VLAN 200, addr=assigned)
```

### Windows in-guest, dry-run

```
== H. Windows VM pre-migration checks + prep ==
  [DRY-RUN] changes are shown as [PLAN] and NOT executed; re-run with --apply

  Windows VM: win-01
  [PASS] [win-01] Guest IP                        — 10.10.20.31
  [PASS] [win-01] Disk provisioning ...           — 2 disk(s), all Basic
  [PASS] [win-01] Secure Boot (guest view)        — guest reports Secure Boot disabled (vSphere firmware=efi)
  [PLAN] [win-01] Hibernation                     — hibernation is ON — [DRY-RUN] would run: powercfg /h off
  [FAIL] [win-01] Fast Startup                    — HiberbootEnabled=1 — Fast Startup is on.
  [PLAN] [win-01] Guest shutdown                  — [DRY-RUN] would run inside guest: shutdown /s /t 0
  [PLAN] [win-01] vSphere power state             — current state = poweredOn; after --apply, expected: poweredOff
```

## Source of truth for the required-permission list

The required-privilege set is baked in at the top of `vma-preflight.py` in the
`VMA_REQUIRED_PRIVILEGES` dict. It mirrors the SpectroCloud VMA "Create Source
Providers" prerequisites (all *Virtual Machine > Interaction* privileges + the
two Snapshot management privileges) with API ids from Broadcom's vSphere 8.0
"Defined Privileges" reference.

The virt-v2v guest OS supportability list is in `V2V_SUPPORTED_GUESTID_PREFIXES`
/ `V2V_UNSUPPORTED_GUESTID_PREFIXES`. Update if new guest OSes are added to
the libguestfs / SpectroCloud verified list.

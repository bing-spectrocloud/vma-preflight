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
| `preflight` | A / B / C / D / E | vCenter auth + all VMA-required vCenter privileges (checked at folder scope by default), ESXi discovery, DNS resolution, TCP 443 to vCenter and 902 to ESXi. Read-only. |
| `allvms` | F / G | Per-VM vSphere-side pre-migration checks: vCenter version for virt-v2v, guest OS supportability, VMware Tools, snapshots, Secure Boot + PXE boot order, CD-ROM / disk / RDM / IDE controller, NIC port group + VLAN mode (incl. trunk / PVLAN / MAC learning). Read-only. |
| `windows` | H | Per-Windows-VM in-guest checks + optional pre-migration prep via WinRM. Auto-skips non-Windows guests when run against a mixed VM list. Changes are **dry-run by default**; pass `--apply` to make them. |
| `all` | A–H | preflight + allvms + windows. |

Exit code is `0` if every check passes, `1` if any FAIL.

## Install

```bash
pip install -r requirements.txt
```

- `pyvmomi` is always required.
- `pywinrm` is required only for `--run windows` (or `--run all` against Windows VMs). **Install it on the jump host where you're running the script, not on the Windows VM itself.**

## Interactive menu (no flags)

```bash
python vma-preflight.py
```

When run with no `--run` flag, the script shows a menu where each option
lists its required / optional flags and a copy-pasteable example command.
After you pick a mode, it prompts for anything that's still missing
(vCenter host, user, password, VM list, Windows credentials, apply mode, etc.).

Flags supplied on the same command line still apply.

### VM input in the interactive prompt

The "VMs to check" prompt accepts any of:

- comma-separated VM names: `web-01,db-01,app-03`
- path to a VM list file: `/home/me/vms.txt`
- `@` prefix: `@/home/me/vms.txt`
- any of the above with surrounding quotes (Linux shell pastes often bring quotes along)

## Common invocations

**vCenter preflight** (checks privileges at folder scope by default):

```bash
python vma-preflight.py --run preflight \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm web-01 --vm db-02 \
    --insecure
```

**All-VMs vSphere-side pre-migration checks** for a specific wave:

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

Dry-run output shows the pending changes as `[PLAN]` lines. Nothing is
written to the guest.

**Windows in-guest checks with changes applied**:

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

**Everything, end to end** (a mixed Linux + Windows wave):

```bash
python vma-preflight.py --run all --apply \
    --vcenter vcenter.corp.example.com \
    --user    svc-vma@vsphere.local \
    --vm      web-linux-01 --vm win-file-01 \
    --win-user Administrator \
    --insecure
```

The Windows section auto-skips Linux VMs (via `guest.guestFamily` /
`config.guestId`), so you don't get spurious WinRM FAILs against them.

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

**Check privileges on a specific inventory folder**:

```bash
python vma-preflight.py --run preflight \
    --vcenter vcenter.corp.example.com --user svc-vma@vsphere.local \
    --folder 'MyDC/vm/Production' \
    --insecure
```

Accepts a full inventory path (`MyDC/vm/Production`) OR a bare folder /
datacenter / cluster name if it's unique in your inventory.

## Privilege-check scope (new default)

Role assignments in vSphere almost always live at a folder, cluster, or
datacenter with "Propagate to children" ticked — so by default the
script checks VMA privileges **at folder scope**, not per VM.

Precedence:

1. If `--folder PATH` is given, check there (repeatable).
2. Otherwise, if `--vm NAME` is given, the script walks each VM's parent
   folder, dedupes, and checks at the folder level. Ten VMs sharing a
   parent folder collapse into one privilege check.
3. If neither is given, check the vCenter root folder.

Entities with identical missing-privilege sets are grouped so a role gap
shared by N entities prints the gap **once**, with the affected entities
listed under it.

Add `--per-vm-privs` to additionally check each VM individually on top of
the folder-scope check — useful for diagnosing broken propagation on a
specific VM (role on the folder, override on the VM).

## Flag reference

### vCenter + scope

| Flag | Purpose |
|---|---|
| `--vcenter HOST` | vCenter endpoint (`host`, `host:port`, `https://host/sdk`). |
| `--user USER` | vCenter username. |
| `--password` \| `VC_PASSWORD` | Password. Prompts securely if neither is set. |
| `--insecure` | Skip TLS verification for self-signed vCenter certs. |
| `--vm NAME` | VM to include (repeatable). |
| `--vm-file PATH` | Text file with one VM name per line. Blank lines and lines starting with `#` are ignored; trailing `# comments` are stripped. Merges with `--vm` (duplicates removed). |
| `--folder PATH` | vSphere inventory path (or bare folder / datacenter / cluster name) to check VMA privileges on. Repeatable. Example: `'MyDC/vm/Production'`. |
| `--per-vm-privs` | ALSO check VMA privileges on each `--vm` individually (default is folder-scope only). |
| `--esxi HOST` | Extra ESXi host for DNS + TCP 902 (repeatable). |
| `--skip-dns` / `--skip-ports` | Trim the preflight to a subset. |
| `--timeout N` | Per-connection timeout in seconds (default 5). |

### Windows guest (WinRM)

| Flag | Purpose |
|---|---|
| `--win-user USER` | Windows guest username (e.g. `Administrator` or `DOMAIN\\svc`). For workgroup Windows guests with NTLM, prefer `.\\Administrator` or `HOSTNAME\\Administrator`. |
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
2. **Fast Startup off** — sets `HiberbootEnabled=0` directly in the
   registry (`HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power`).
   Done separately because Group Policy / vendor images can re-set it to 1
   independently of hibernation state. Verifies the value is `0` after setting.
3. **Clean guest shutdown** — `shutdown /s /t 0` inside the guest, then
   polling `vm.runtime.powerState` in vSphere for up to 120 seconds.

No files, services, drivers, or registry keys other than those touched by
these three operations are modified.

## WinRM pre-check and prerequisites on the Windows guest

Before any Windows check runs, the script does two cheap probes against
each Windows VM:

1. **TCP port probe** of 5985 (or 5986) — fails fast with concrete
   remediation if the WinRM listener isn't up on the guest.
2. **Auth probe** (`$env:COMPUTERNAME`) — catches 401 / Unauthorized
   (bad password, or NTLM disabled on the guest) and TLS issues before the
   real checks fire, so you don't get a wall of pywinrm/urllib3 traceback.

On the target Windows VM you need:

```powershell
# Elevated PowerShell on the Windows guest
Set-NetConnectionProfile -NetworkCategory Private   # if on Public net
winrm quickconfig -force
Get-Service WinRM                                   # confirm 'Running'
Enable-PSRemoting -Force                            # if quickconfig complained

# Firewall (if not already opened by quickconfig)
New-NetFirewallRule -Name "WinRM-HTTP" -DisplayName "WinRM HTTP" `
    -Enabled True -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow
```

For NTLM auth from a non-domain jump host to a workgroup Windows guest, use
`--win-user '.\Administrator'` (or `HOSTNAME\Administrator`).

## Sample output snippets

### VMA-required vCenter privileges — folder-scope default

```
== B. vCenter privileges required by VMA ==
  [PASS] Authenticated principal  — svc-vma@vsphere.local
  [FAIL] Missing privileges on Folder 'Production' (path 'MyDC/vm/Production', inferred from 10 VM(s): web-01, web-02, web-03, db-01, db-02, +5 more)  — 23 of 28 missing
      - VirtualMachine.Interact.AnswerQuestion  (Interaction > Answer question)
      - VirtualMachine.Interact.Backup  (Interaction > Backup operation on virtual machine)
      ...
```

One check, one list of missing permissions, all 10 VMs covered.

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

### Windows in-guest, mixed Linux + Windows list

```
== H. Windows VM pre-migration checks + prep ==
  [DRY-RUN] changes are shown as [PLAN] and NOT executed; re-run with --apply

  Windows VM: win-01  (detected: guestFamily=windowsGuest, 'Microsoft Windows Server 2019 (64-bit)')
  [PASS] [win-01] Guest IP                        — 10.10.20.31
  [PASS] [win-01] WinRM port TCP 10.10.20.31:5985 — listener reachable
  [PASS] [win-01] WinRM auth                      — authenticated as WIN-01
  [PASS] [win-01] Disk provisioning ...           — 2 disk(s), all Basic
  [PASS] [win-01] Secure Boot (guest view)        — guest reports Secure Boot disabled (vSphere firmware=efi)
  [PLAN] [win-01] Hibernation                     — hibernation is ON — [DRY-RUN] would run: powercfg /h off
  [PLAN] [win-01] Fast Startup                    — HiberbootEnabled=1 — [DRY-RUN] would set it to 0
  [PLAN] [win-01] Guest shutdown                  — [DRY-RUN] would run inside guest: shutdown /s /t 0
  [PLAN] [win-01] vSphere power state             — current state = poweredOn; after --apply, expected: poweredOff
  [SKIP] [db-linux-01] Windows checks             — guest is not Windows (guestFamily=linuxGuest, 'Ubuntu Linux (64-bit)')
```

## Summary line

The summary tallies six statuses:

- `PASS` — check succeeded
- `FAIL` — check failed; exit code 1
- `WARN` — non-blocking issue, review before migrating
- `SKIP` — not applicable (e.g., Windows checks on a Linux VM)
- `PLAN` — change that WOULD run under `--apply`
- `DONE` — change that was applied

```
== Summary ==
  PASS: 27   FAIL: 0   WARN: 1   SKIP: 1   PLAN: 3   DONE: 0

3 pending change(s) shown as [PLAN]. Re-run with --apply to execute them.
1 warning(s) — review before starting migration.
```

## Source of truth

- `VMA_REQUIRED_PRIVILEGES` (top of `vma-preflight.py`) — full set of
  vCenter privileges the VMA source provider needs, drawn from the
  SpectroCloud "Create Source Providers" prerequisites. Update if
  SpectroCloud revises the list.
- `V2V_SUPPORTED_GUESTID_PREFIXES` / `V2V_UNSUPPORTED_GUESTID_PREFIXES`
  — virt-v2v guest-OS supportability allow/deny lists derived from
  libguestfs + the SpectroCloud verified list.
- vCenter version compatibility (`vcenter_version_ok_for_v2v`) — VMA
  currently supports vSphere 7.0 and 8.0. 9.x reports as WARN until
  SpectroCloud verifies it.

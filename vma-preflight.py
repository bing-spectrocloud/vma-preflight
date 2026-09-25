#!/usr/bin/env python3
"""
Palette VMO / VMA (VM Migration Assistant) environment pre-flight checker.

Validates that an environment is ready for the SpectroCloud Palette Virtual
Machine Migration Assistant to migrate VMs from VMware vSphere to KubeVirt (VMO).

Checks performed (per SpectroCloud docs, aligned to the VMA "Create Source
Providers" prerequisites):

  A. vCenter endpoint is reachable and credentials are valid
  B. The vCenter user has every privilege VMA requires; each missing
     privilege is listed by API id AND UI name so operators can fix it
  C. DNS resolution of the vCenter FQDN and each ESXi host that hosts a VM
     to be migrated (works from an operator laptop OR from inside the VMA
     pod via kubectl exec, so it reflects the real cluster network)
  D. TCP 443 to vCenter and TCP 902 to each ESXi host with VMs to migrate

Usage (laptop):

    pip install pyvmomi
    python vma-preflight.py \\
        --vcenter vcenter.corp.example.com \\
        --user   svc-vma@vsphere.local \\
        --vm     web-01 --vm db-02 \\
        --insecure

Usage (inside the VMA pod):

    kubectl -n <vma-ns> cp vma-preflight.py <vma-pod>:/tmp/vma-preflight.py
    kubectl -n <vma-ns> exec -it <vma-pod> -- \\
        python3 /tmp/vma-preflight.py --vcenter vcenter.corp.example.com \\
        --user svc-vma@vsphere.local --vm web-01 --insecure

Exit status: 0 if all checks pass, 1 if any FAIL.
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Colored output helpers
# ---------------------------------------------------------------------------
class C:
    _tty = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    RED = "\033[31m" if _tty else ""
    GRN = "\033[32m" if _tty else ""
    YLW = "\033[33m" if _tty else ""
    CYN = "\033[36m" if _tty else ""
    BLD = "\033[1m" if _tty else ""
    RST = "\033[0m" if _tty else ""


RESULTS: List[Tuple[str, str, str, str]] = []
_section = ""


def _record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((_section, name, status, detail))
    tag = {
        "PASS": f"{C.GRN}[PASS]{C.RST}",
        "FAIL": f"{C.RED}[FAIL]{C.RST}",
        "WARN": f"{C.YLW}[WARN]{C.RST}",
        "SKIP": f"{C.CYN}[SKIP]{C.RST}",
    }[status]
    line = f"  {tag} {name}"
    if detail:
        line += f"  — {detail}"
    print(line)


def PASS(name: str, detail: str = "") -> None: _record(name, "PASS", detail)
def FAIL(name: str, detail: str = "") -> None: _record(name, "FAIL", detail)
def WARN(name: str, detail: str = "") -> None: _record(name, "WARN", detail)
def SKIP(name: str, detail: str = "") -> None: _record(name, "SKIP", detail)


def head(name: str) -> None:
    global _section
    _section = name
    print(f"\n{C.BLD}== {name} =={C.RST}")


# ---------------------------------------------------------------------------
# VMA-required vCenter privileges
#
# Source: https://docs.spectrocloud.com/vm-management/vm-migration-assistant/
#         create-source-providers/  (Prerequisites -> vCenter user privileges)
#
# The docs require:
#   - Virtual Machine Interaction Privileges (ALL entries in that category)
#   - Virtual machine.Snapshot management.Create snapshot
#   - Virtual machine.Snapshot management.Remove Snapshot
#
# The Virtual Machine Interaction category is expanded from the VMware 8.0
# "Defined Privileges" reference (Broadcom TechDocs).
# ---------------------------------------------------------------------------
VMA_REQUIRED_PRIVILEGES: dict[str, str] = {
    # Virtual Machine > Interaction (all)
    "VirtualMachine.Interact.AnswerQuestion":       "Interaction > Answer question",
    "VirtualMachine.Interact.Backup":               "Interaction > Backup operation on virtual machine",
    "VirtualMachine.Interact.SetCDMedia":           "Interaction > Configure CD media",
    "VirtualMachine.Interact.SetFloppyMedia":       "Interaction > Configure floppy media",
    "VirtualMachine.Interact.ConsoleInteract":      "Interaction > Console interaction",
    "VirtualMachine.Interact.CreateScreenshot":     "Interaction > Create screenshot",
    "VirtualMachine.Interact.DefragmentAllDisks":   "Interaction > Defragment all disks",
    "VirtualMachine.Interact.DeviceConnection":     "Interaction > Device connection",
    "VirtualMachine.Interact.DnD":                  "Interaction > Drag and Drop",
    "VirtualMachine.Interact.GuestControl":         "Interaction > Guest operating system management by VIX API",
    "VirtualMachine.Interact.PutUsbScanCodes":      "Interaction > Inject USB HID scan codes",
    "VirtualMachine.Interact.Pause":                "Interaction > Pause or Unpause",
    "VirtualMachine.Interact.SESparseMaintenance":  "Interaction > Perform wipe or shrink operations",
    "VirtualMachine.Interact.PowerOff":             "Interaction > Power Off",
    "VirtualMachine.Interact.PowerOn":              "Interaction > Power On",
    "VirtualMachine.Interact.Record":               "Interaction > Record session on Virtual Machine",
    "VirtualMachine.Interact.Replay":               "Interaction > Replay session on Virtual Machine",
    "VirtualMachine.Interact.Reset":                "Interaction > Reset",
    "VirtualMachine.Interact.EnableSecondary":      "Interaction > Resume Fault Tolerance",
    "VirtualMachine.Interact.Suspend":              "Interaction > Suspend",
    "VirtualMachine.Interact.DisableSecondary":     "Interaction > Suspend Fault Tolerance / Test restart Secondary VM",
    "VirtualMachine.Interact.SuspendToMemory":      "Interaction > Suspend to memory",
    "VirtualMachine.Interact.MakePrimary":          "Interaction > Test failover",
    "VirtualMachine.Interact.TurnOffFaultTolerance": "Interaction > Turn Off Fault Tolerance",
    "VirtualMachine.Interact.CreateSecondary":      "Interaction > Turn On Fault Tolerance",
    "VirtualMachine.Interact.ToolsInstall":         "Interaction > VMware Tools install",
    # Virtual Machine > Snapshot management (explicitly required by VMA)
    "VirtualMachine.State.CreateSnapshot":          "Snapshot management > Create snapshot",
    "VirtualMachine.State.RemoveSnapshot":          "Snapshot management > Remove Snapshot",
}


# ---------------------------------------------------------------------------
# Endpoint parsing
# ---------------------------------------------------------------------------
def parse_endpoint(endpoint: str) -> Tuple[str, int]:
    """Accept 'vcenter.example.com', 'vcenter.example.com:443',
    or 'https://vcenter.example.com/sdk'. Returns (host, port)."""
    endpoint = endpoint.strip()
    if "://" in endpoint:
        p = urlparse(endpoint)
        host = p.hostname or ""
        port = p.port or (443 if (p.scheme or "https").lower() == "https" else 80)
    elif endpoint.count(":") == 1:
        host, port_s = endpoint.split(":", 1)
        port = int(port_s)
    else:
        host, port = endpoint, 443
    if not host:
        raise ValueError(f"Could not parse host from endpoint: {endpoint!r}")
    return host, port


# ---------------------------------------------------------------------------
# vCenter checks (needs pyvmomi)
# ---------------------------------------------------------------------------
def check_vcenter(
    host: str,
    port: int,
    user: str,
    password: str,
    insecure: bool,
    vm_names: Optional[Sequence[str]],
    timeout: float,
) -> Optional[List[str]]:
    """Connect + auth + privilege check + ESXi discovery.

    Returns list of ESXi host names discovered (may be empty), or None if the
    connect/auth step failed hard.
    """
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim, vmodl

    if insecure:
        ssl_ctx = ssl._create_unverified_context()
    else:
        ssl_ctx = ssl.create_default_context()

    # Give SmartConnect a socket-level timeout ceiling.
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(max(timeout, 15.0))

    head("A. vCenter connectivity & authentication")
    si = None
    try:
        si = SmartConnect(
            host=host,
            user=user,
            pwd=password,
            port=port,
            sslContext=ssl_ctx,
            connectionPoolTimeout=int(max(timeout, 15.0)),
        )
        PASS(f"Authenticated to vCenter {host}:{port} as {user}")
    except vim.fault.InvalidLogin:
        FAIL(f"Auth to {host}:{port}", "invalid login (user/password rejected)")
        return None
    except socket.gaierror as e:
        FAIL(f"Connect to {host}:{port}", f"DNS/host error: {e}")
        return None
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        FAIL(f"Connect to {host}:{port}", f"network error: {e}")
        return None
    except ssl.SSLError as e:
        FAIL(f"TLS to {host}:{port}", f"{e}. If vCenter uses a self-signed cert, re-run with --insecure.")
        return None
    except vmodl.MethodFault as e:
        FAIL(f"vCenter API error at {host}:{port}", str(e.msg))
        return None
    except Exception as e:  # noqa: BLE001
        FAIL(f"Unexpected error connecting to {host}:{port}", f"{type(e).__name__}: {e}")
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)

    try:
        content = si.RetrieveContent()
        about = content.about
        PASS("vCenter info", f"{about.fullName} (API {about.apiVersion}, {about.osType})")
    except Exception as e:  # noqa: BLE001
        WARN("Fetch vCenter about info", f"{type(e).__name__}: {e}")
        content = si.RetrieveContent()

    # ---- Privilege validation ----
    head("B. vCenter privileges required by VMA")
    _check_privileges(content, vm_names)

    # ---- ESXi discovery ----
    head("C. ESXi host discovery (for VMs to migrate)")
    esxi_hosts = _discover_esxi_hosts(content, vm_names)

    try:
        Disconnect(si)
    except Exception:
        pass
    return esxi_hosts


def _find_vm(content, name: str):
    from pyVmomi import vim
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        for vm in view.view:
            if vm.name == name:
                return vm
    finally:
        view.Destroy()
    return None


def _check_privileges(content, vm_names: Optional[Sequence[str]]) -> bool:
    from pyVmomi import vim

    auth_mgr = content.authorizationManager
    session_mgr = content.sessionManager
    try:
        session = session_mgr.currentSession
        user = session.userName
        session_key = session.key
    except Exception as e:  # noqa: BLE001
        FAIL("Fetch current session", str(e))
        return False
    PASS("Authenticated principal", user)

    # Target entities: specific VMs if given, otherwise the root folder.
    entities: List[Tuple[str, object]] = []
    if vm_names:
        for name in vm_names:
            vm = _find_vm(content, name)
            if vm is not None:
                entities.append((f"VM '{name}'", vm))
            else:
                WARN(f"Locate VM '{name}'", "not found in inventory; skipping per-VM permission check")
    if not entities:
        entities.append(("vCenter root folder", content.rootFolder))

    priv_ids = list(VMA_REQUIRED_PRIVILEGES.keys())
    all_missing: dict[str, List[str]] = {}

    for label, ent in entities:
        try:
            result = auth_mgr.HasPrivilegeOnEntity(
                entity=ent,
                sessionId=session_key,
                privId=priv_ids,
            )
        except vim.fault.NoPermission:
            FAIL(
                f"Privilege check on {label}",
                "user has no permission to inspect authorization on this entity "
                "(assign at least System.View at this scope)",
            )
            continue
        except Exception as e:  # noqa: BLE001
            FAIL(f"Privilege check on {label}", f"{type(e).__name__}: {e}")
            continue

        missing = [priv_ids[i] for i, has in enumerate(result) if not has]
        if not missing:
            PASS(f"All {len(priv_ids)} required privileges present on {label}")
        else:
            all_missing[label] = missing

    if not all_missing:
        return True

    for label, missing in all_missing.items():
        FAIL(f"Missing privileges on {label}", f"{len(missing)} of {len(priv_ids)} missing")
        for pid in missing:
            print(f"      - {C.RED}{pid}{C.RST}  ({VMA_REQUIRED_PRIVILEGES[pid]})")
    return False


def _discover_esxi_hosts(content, vm_names: Optional[Sequence[str]]) -> List[str]:
    from pyVmomi import vim

    hosts: set[str] = set()
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        vm_filter = set(vm_names) if vm_names else None
        for vm in view.view:
            if vm_filter is not None and vm.name not in vm_filter:
                continue
            try:
                h = vm.runtime.host
                if h and h.name:
                    hosts.add(h.name)
            except Exception:
                continue
    finally:
        view.Destroy()

    if not vm_names:
        if hosts:
            PASS("Discovered ESXi hosts across all VMs", f"{len(hosts)} host(s)")
        else:
            WARN("No ESXi hosts discovered", "vCenter reports no VMs with runtime.host")
    else:
        for name in vm_names:
            # We can't tell without another pass which host each named VM is on,
            # so just report the union.
            pass
        if hosts:
            PASS(f"Discovered ESXi hosts for {len(vm_names)} named VM(s)", ", ".join(sorted(hosts)))
        else:
            WARN(
                "No ESXi hosts discovered for named VMs",
                "check --vm names; falling back to skipping ESXi port checks",
            )
    return sorted(hosts)


# ---------------------------------------------------------------------------
# DNS resolution
# ---------------------------------------------------------------------------
def check_dns(names: Iterable[str]) -> None:
    head("D. DNS resolution")
    for name in names:
        # Skip literal IPs (resolution is a no-op).
        try:
            socket.inet_pton(socket.AF_INET, name)
            PASS(f"Resolve {name}", "literal IPv4 address")
            continue
        except (OSError, ValueError):
            pass
        try:
            socket.inet_pton(socket.AF_INET6, name)
            PASS(f"Resolve {name}", "literal IPv6 address")
            continue
        except (OSError, ValueError):
            pass
        try:
            addrs = socket.getaddrinfo(name, None)
            ips = sorted({a[4][0] for a in addrs})
            PASS(f"Resolve {name}", ", ".join(ips))
        except socket.gaierror as e:
            FAIL(f"Resolve {name}", f"{e}")


# ---------------------------------------------------------------------------
# TCP port reachability
# ---------------------------------------------------------------------------
def _tcp_ok(host: str, port: int, timeout: float) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.gaierror as e:
        return False, f"DNS: {e}"
    except socket.timeout:
        return False, f"timeout after {timeout}s"
    except ConnectionRefusedError:
        return False, "connection refused"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def check_ports(targets: Sequence[Tuple[str, int]], timeout: float) -> None:
    head(f"E. TCP port reachability (timeout {timeout:g}s)")
    if not targets:
        SKIP("Port reachability", "no targets")
        return
    max_workers = min(16, len(targets))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_tcp_ok, h, p, timeout): (h, p) for h, p in targets}
        for fut in as_completed(futs):
            h, p = futs[fut]
            ok, err = fut.result()
            label = f"TCP {h}:{p}"
            if ok:
                PASS(label)
            else:
                FAIL(label, err)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def summary() -> int:
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for _, _, s, _ in RESULTS:
        counts[s] += 1
    print(f"\n{C.BLD}== Summary =={C.RST}")
    print(
        f"  {C.GRN}PASS: {counts['PASS']}{C.RST}   "
        f"{C.RED}FAIL: {counts['FAIL']}{C.RST}   "
        f"{C.YLW}WARN: {counts['WARN']}{C.RST}   "
        f"{C.CYN}SKIP: {counts['SKIP']}{C.RST}"
    )

    fails = [(sec, n, d) for sec, n, s, d in RESULTS if s == "FAIL"]
    if fails:
        print(f"\n{C.RED}Failures:{C.RST}")
        for sec, n, d in fails:
            suffix = f"  — {d}" if d else ""
            print(f"  - [{sec}] {n}{suffix}")
        print(f"\n{C.RED}Environment is NOT ready for VMA.{C.RST}")
        return 1

    print(f"\n{C.GRN}All checks passed. Environment looks ready for VMA.{C.RST}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vma-preflight",
        description=(
            "Palette VMO / VMA environment pre-flight checker: validates vCenter "
            "auth + required privileges, DNS resolution, and TCP port reachability "
            "to vCenter (443) and ESXi hosts (902)."
        ),
    )
    p.add_argument("--vcenter", help="vCenter endpoint: host, host:port, or https://host/sdk")
    p.add_argument("--user", help="vCenter username (e.g. svc-vma@vsphere.local)")
    p.add_argument(
        "--password",
        help="vCenter password. Prefers env VC_PASSWORD; prompts if neither is set.",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (use for self-signed vCenter certs).",
    )
    p.add_argument(
        "--vm",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Name of a VM to migrate. Repeatable. When set, privilege checks are "
            "evaluated against each named VM and ESXi discovery is limited to "
            "these VMs' hosts."
        ),
    )
    p.add_argument(
        "--esxi",
        action="append",
        default=[],
        metavar="HOST",
        help="Extra ESXi host to include in DNS + port 902 checks. Repeatable.",
    )
    p.add_argument("--skip-vcenter", action="store_true", help="Skip vCenter auth + privilege checks.")
    p.add_argument("--skip-dns", action="store_true", help="Skip DNS resolution checks.")
    p.add_argument("--skip-ports", action="store_true", help="Skip TCP port reachability checks.")
    p.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-connection timeout in seconds for TCP + vCenter connect (default 5).",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    print(f"{C.BLD}Palette VMO / VMA Pre-flight{C.RST}")
    print(f"  Run at: {time.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}")
    print(f"  Host:   {socket.gethostname()}")

    vcenter_host: Optional[str] = None
    vcenter_port: int = 443
    if args.vcenter:
        try:
            vcenter_host, vcenter_port = parse_endpoint(args.vcenter)
        except ValueError as e:
            head("A. vCenter connectivity & authentication")
            FAIL("Parse --vcenter", str(e))
            return summary()

    esxi_hosts: List[str] = list(dict.fromkeys(args.esxi))  # de-dup, keep order

    # ---- A + B + partial C: vCenter connect / auth / privileges / discovery
    if not args.skip_vcenter:
        if not args.vcenter or not args.user:
            head("A. vCenter connectivity & authentication")
            SKIP("vCenter check", "requires --vcenter and --user")
        else:
            try:
                import pyVim.connect  # noqa: F401
            except ImportError:
                head("A. vCenter connectivity & authentication")
                FAIL(
                    "pyvmomi not installed",
                    "install with: pip install pyvmomi  (or pip3 install --user pyvmomi)",
                )
                return summary()

            password = args.password or os.environ.get("VC_PASSWORD")
            if not password:
                try:
                    password = getpass.getpass(f"vCenter password for {args.user}: ")
                except (EOFError, KeyboardInterrupt):
                    head("A. vCenter connectivity & authentication")
                    FAIL("Password prompt", "no password provided (use --password or VC_PASSWORD)")
                    return summary()

            discovered = check_vcenter(
                host=vcenter_host,
                port=vcenter_port,
                user=args.user,
                password=password,
                insecure=args.insecure,
                vm_names=args.vm or None,
                timeout=args.timeout,
            )
            if discovered:
                for h in discovered:
                    if h not in esxi_hosts:
                        esxi_hosts.append(h)

    # ---- D: DNS
    if not args.skip_dns:
        names: List[str] = []
        if vcenter_host:
            names.append(vcenter_host)
        for h in esxi_hosts:
            if h and h not in names:
                names.append(h)
        if names:
            check_dns(names)
        else:
            head("D. DNS resolution")
            SKIP("DNS resolution", "no vCenter or ESXi hosts to resolve")

    # ---- E: TCP ports
    if not args.skip_ports:
        targets: List[Tuple[str, int]] = []
        if vcenter_host:
            targets.append((vcenter_host, vcenter_port or 443))
        for h in esxi_hosts:
            targets.append((h, 902))
        check_ports(targets, args.timeout)

    return summary()


if __name__ == "__main__":
    sys.exit(main())

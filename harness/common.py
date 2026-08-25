#!/usr/bin/env python3
"""Shared infrastructure for LinHT-rfic validation harnesses.

Host-side orchestration only: VM access over ssh (no scp on the guest — transfer
is cat-over-ssh per CUA-AGENTS.md), container access via docker exec, license
TCP probes, JSON emission. No secrets stored here: the VM password comes from
$LINHT_VM_PASS or is parsed from the host-side house script at runtime.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HOST_RUN_ADS = Path.home() / "exp/rfic/cadence/run-ads.sh"
VM_HOSTPORT = ("127.0.0.1", 2222)
VM_USER = "rakesh"
CONTAINER = "linht_iic"

LICENSE_SERVERS = {
    "cadence-dli": ("10.180.60.103", 5280),
    "siemens-dli": ("10.180.60.101", 1717),
    "synopsys-dli": ("10.180.60.102", 27020),
    "keysight-eda": ("10.180.60.104", 27009),
}

REPO_ROOT = Path(__file__).resolve().parent.parent

# Canonical ADS2027 environment for the VM (LD_LIBRARY_PATH must include
# tools/python/lib — the libpython trap; ~/exp/sims/ADS-AGENTS.md §3).
ADS_ENV_TEXT = (
    "export ADS=/opt/ads/ADS2027\n"
    'export HPEESOF_DIR="$ADS" EESOF_LICENSE_FILE=27009@10.180.60.104\n'
    'export EESOFLIC="$HOME/.eesoflic" LC_ALL=C\n'
    'export PATH="$ADS/bin:$PATH"\n'
    'export LD_LIBRARY_PATH="$ADS/lib/linux_x86_64:'
    '$ADS/circuit/lib.linux_x86_64:$ADS/fem/2027.00/linux_x86_64/bin:'
    '$ADS/adsptolemy/lib.linux_x86_64:$ADS/tools/python/lib"\n'
)


class StageError(RuntimeError):
    """Failure carrying the exact stage that failed, e.g. 'transfer:vm-push'."""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"[{stage}] {detail}")
        self.stage = stage
        self.detail = detail


def _vm_pass() -> str:
    pw = os.environ.get("LINHT_VM_PASS")
    if pw:
        return pw
    try:  # house convention: password lives in host-side scripts outside this repo
        text = HOST_RUN_ADS.read_text()
        for tok in text.split():
            if tok.startswith("-p") and len(tok) > 12:
                return tok[2:]
        import re
        m = re.search(r"sshpass\s+-p\s+(\S+)", text)
        if m:
            return m.group(1)
    except OSError:
        pass
    raise StageError("access:vm-pass", "set LINHT_VM_PASS; no house script found")


def _ssh_cmd(remote_cmd: str, timeout: float) -> list[str]:
    return [
        "sshpass", "-p", _vm_pass(), "ssh",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={min(10, int(timeout))}",
        "-p", str(VM_HOSTPORT[1]), f"{VM_USER}@{VM_HOSTPORT[0]}", remote_cmd,
    ]


def vm_ssh(remote_cmd: str, timeout: float = 60.0, check: bool = False) -> tuple[int, str]:
    """Run a command on the VM; returns (rc, combined output)."""
    proc = subprocess.run(_ssh_cmd(remote_cmd, timeout), capture_output=True,
                          text=True, timeout=timeout)
    out = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise StageError("vm-ssh", f"rc={proc.returncode}: {out[-400:]}")
    return proc.returncode, out


def vm_push(local: Path, remote_path: str, timeout: float = 120.0) -> None:
    """Copy one file to the VM via cat-over-ssh (scp/sftp disabled on guest)."""
    data = local.read_bytes()
    cmd = _ssh_cmd(f"cat > '{remote_path}'", timeout)
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise StageError(
            "transfer:vm-push",
            (proc.stderr or b"?").decode(errors="replace")[-300:])


def vm_pull(remote_path: str, timeout: float = 120.0) -> bytes:
    cmd = _ssh_cmd(f"cat '{remote_path}'", timeout)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise StageError("transfer:vm-pull",
                         (proc.stderr or b"?").decode(errors="replace")[-300:])
    return proc.stdout


def container_exec(remote_cmd: str, timeout: float = 120.0) -> tuple[int, str]:
    """Run a command inside the linht_iic container."""
    proc = subprocess.run(["docker", "exec", CONTAINER, "bash", "-lc", remote_cmd],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def tcp_open(host: str, port: int, timeout: float = 4.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check(name: str, ok: bool, detail: str) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


def emit(results: list[dict], meta: dict | None = None) -> int:
    doc = {"meta": meta or {}, "results": results,
           "ok": all(r["ok"] for r in results)}
    print(json.dumps(doc, indent=2))
    return 0 if doc["ok"] else 1


def eprint(*a):
    print(*a, file=sys.stderr)

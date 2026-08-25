#!/usr/bin/env python3
"""G001 environment-and-access probe.

Emits JSON verdicts for: host VPN routes, four license servers, VM reachability,
commercial tool versions (hpeesofsim/Momentum, Spectre, Virtuoso, Calibre),
SG13G2 model presence on the VM and in the container, container PDK and
repo-mount health. Exit 0 iff every check passes.

--self-test exercises pure logic without touching VM/licenses (CI-safe).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import LICENSE_SERVERS, StageError, check, container_exec, emit, tcp_open, vm_ssh

# Canonical ADS2027 environment for the VM (LD_LIBRARY_PATH must include
# tools/python/lib — the libpython trap; see ~/exp/sims/ADS-AGENTS.md §3).
ADS_ENV = (
    'export ADS=/opt/ads/ADS2027; '
    'export HPEESOF_DIR="$ADS" EESOF_LICENSE_FILE=27009@10.180.60.104 '
    'EESOFLIC="$HOME/.eesoflic" LC_ALL=C; '
    'export PATH="$ADS/bin:$PATH"; '
    'export LD_LIBRARY_PATH="$ADS/lib/linux_x86_64:$ADS/circuit/lib.linux_x86_64:'
    '$ADS/fem/2027.00/linux_x86_64/bin:$ADS/adsptolemy/lib.linux_x86_64:'
    '$ADS/tools/python/lib"'
)

# One batched remote script: each probe prints KEY=VALUE; parse host-side.
VM_PROBE = f"""
{ADS_ENV}
echo HOST=$(hostname)
ls -d /opt/ads/ADS2027 >/dev/null 2>&1 && echo ADS_DIR=ok || echo ADS_DIR=missing
HPEESOF_VER=$(hpeesofsim -v 2>&1 | tr '\\n' ' '); echo HPEESOF_VER=$HPEESOF_VER
[ -x /opt/ads/ADS2027/bin/adsMomWrapper ] && echo MOM=ok || echo MOM=missing
SPECTRE_VER=$(/opt/cadence/installs/SPECTRE251/bin/spectre -V 2>&1 | head -1); echo SPECTRE_VER=$SPECTRE_VER
VIRTUOSO_VER=$(/opt/cadence/installs/IC251/bin/virtuoso -V 2>&1 | head -1 | tr -d '"'); echo VIRTUOSO_VER=$VIRTUOSO_VER
CALIBRE_VER=$(/opt/siemens/calibre/bin/calibre -version 2>&1 | grep -i calibre | head -2 | tr '\\n' ' '); echo CALIBRE_VER=$CALIBRE_VER
[ -f ~/linht/ads/pdk/sg13g2_tt.net ] && echo SG13_ADS_MODELS=ok || echo SG13_ADS_MODELS=missing
grep -q 'psp103' ~/linht/ads/pdk/va/*.va 2>/dev/null && echo PSP_VA=ok || echo PSP_VA=unknown
"""


def parse_kv(text: str) -> dict[str, str]:
    kv = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("Warning"):
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv


def assemble(results: list[dict]) -> dict:
    return {"results": results, "ok": all(r["ok"] for r in results)}


def self_test() -> int:
    """Pure-logic checks only: parsing, verdict assembly, error staging."""
    sample = "HOST=samjna.asnaviram\nADS_DIR=ok\nHPEESOF_VER=\nMOM=ok\n"
    kv = parse_kv(sample)
    assert kv["HOST"] == "samjna.asnaviram", kv
    assert kv["ADS_DIR"] == "ok" and kv["HPEESOF_VER"] == "", kv
    assert check("x", True, "d") == {"check": "x", "ok": True, "detail": "d"}
    try:
        raise StageError("run:hpeesofsim", "boom")
    except StageError as e:
        assert e.stage == "run:hpeesofsim" and "boom" in str(e)
    doc_ok = assemble([check("a", True, ""), check("b", True, "")])
    assert doc_ok["ok"] is True and len(doc_ok["results"]) == 2
    doc_bad = assemble([check("a", True, ""), check("b", False, "why")])
    assert doc_bad["ok"] is False
    print(json.dumps({"self_test": "pass"}, indent=2))
    return 0


def run() -> int:
    results: list[dict] = []

    # --- host-side ---
    rt = subprocess.run(["ip", "-o", "route", "show"], capture_output=True,
                        text=True).stdout
    # VPN installs per-host /32s via tun0 (no /24 aggregate) — probe one server IP
    results.append(check("host.vpn-route",
                         "10.180.60.101" in rt and "tun" in rt,
                         rt.strip()[:200] or "no routes"))
    for name, (h, p) in LICENSE_SERVERS.items():
        results.append(check(f"license.{name}", tcp_open(h, p), f"{h}:{p}"))

    # --- VM-side (one ssh round trip) ---
    try:
        rc, out = vm_ssh(VM_PROBE, timeout=120)
        kv = parse_kv(out)
        results.append(check("vm.ssh", rc == 0 and "HOST=" in out,
                             kv.get("HOST", out[-120:])))
        results.append(check("vm.ads2027-dir", kv.get("ADS_DIR") == "ok",
                             kv.get("ADS_DIR", "?")))
        results.append(check("vm.hpeesofsim-version",
                             bool(kv.get("HPEESOF_VER"))
                             and "error while loading" not in kv.get("HPEESOF_VER", ""),
                             kv.get("HPEESOF_VER", "")[:100] or "no version banner"))
        results.append(check("vm.momentum-wrapper", kv.get("MOM") == "ok",
                             kv.get("MOM", "?")))
        results.append(check("vm.spectre",
                             "version" in kv.get("SPECTRE_VER", "").lower(),
                             kv.get("SPECTRE_VER", "")[:100]))
        results.append(check("vm.virtuoso",
                             "version" in kv.get("VIRTUOSO_VER", "").lower(),
                             kv.get("VIRTUOSO_VER", "")[:100]))
        results.append(check("vm.calibre", bool(kv.get("CALIBRE_VER")),
                             kv.get("CALIBRE_VER", "")[:160]))
        results.append(check("vm.sg13g2-ads-models",
                             kv.get("SG13_ADS_MODELS") == "ok",
                             "~/linht/ads/pdk/sg13g2_tt.net"))
        results.append(check("vm.psp103-veriloga", kv.get("PSP_VA") == "ok",
                             "~/linht/ads/pdk/va/psp103.va"))
    except StageError as e:
        for extra in ("ads2027-dir", "hpeesofsim-version", "momentum-wrapper",
                      "spectre", "virtuoso", "calibre", "sg13g2-ads-models",
                      "psp103-veriloga"):
            results.append(check(f"vm.{extra}", False, "skipped: vm unreachable"))

    # --- container-side ---
    rc, out = container_exec(
        "/foss/tools/ngspice/bin/ngspice --version 2>&1 | grep -m1 ngspice; "
        "ls -d $PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/models >/dev/null 2>&1 "
        "&& echo PDK=ok || echo PDK=missing; "
        "touch /foss/designs/LinHT_IC/.rw_probe 2>/dev/null && "
        "{ rm /foss/designs/LinHT_IC/.rw_probe; echo REPO_RW=ok; } || echo REPO_RW=fail",
        timeout=60)
    ng_lines = [l for l in out.splitlines()
                if "ngspice" in l.lower() or "written by" in l.lower()]
    ng = ng_lines[0] if ng_lines else out[:100]
    results.append(check("container.ngspice", bool(ng_lines), ng))
    ckv = dict(l.split("=", 1) for l in out.splitlines()
               if "=" in l and not l.startswith("[INFO]"))
    results.append(check("container.pdk", ckv.get("PDK") == "ok", ckv.get("PDK", "?")))
    results.append(check("container.repo-rw", ckv.get("REPO_RW") == "ok",
                         ckv.get("REPO_RW", "?")))

    meta = {"probe": "env_probe",
            "note": "calibre license var unverified by design; first DRC doubles as discovery"}
    return emit(results, meta)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(run())

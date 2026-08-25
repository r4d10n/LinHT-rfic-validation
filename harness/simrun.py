#!/usr/bin/env python3
"""G003 circuit cross-check harness: one testbench, three simulators.

Reference of record: ngspice running the repo deck unmodified in the
linht_iic container (its .control/wrdata CSV is parsed directly).
Cross-checks: hpeesofsim on the VM (netadapt --target ads deck, MDS raw
output parsed by tools/rawread_ads.py) and Spectre (netadapt --target
spectre deck, psfascii output).

Stages are named adapt/push/run/parse/compare so failures identify exactly
where they broke (AGENTS.md contract).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, StageError, container_exec, emit, vm_pull, vm_ssh, _vm_pass
from netadapt import adapt

EVIDENCE = REPO_ROOT / "evidence"
VM_WORKROOT = "~/linht_val"
ADS_ENV = (
    'export ADS=/opt/ads/ADS2027; '
    'export HPEESOF_DIR="$ADS" EESOF_LICENSE_FILE=27009@10.180.60.104 '
    'EESOFLIC="$HOME/.eesoflic" LC_ALL=C; '
    'export PATH="$ADS/bin:$PATH"; '
    'export LD_LIBRARY_PATH="$ADS/lib/linux_x86_64:$ADS/circuit/lib.linux_x86_64:'
    '$ADS/fem/2027.00/linux_x86_64/bin:$ADS/adsptolemy/lib.linux_x86_64:'
    '$ADS/tools/python/lib"'
)

# canonical signal -> per-tool vector names (ads/spectre spellings verified
# against first live outputs; adjust there if a tool renames)
DEFAULT_SIGNALS = {
    "lo_i": {"ng": "v(lo_i)", "ads": "lo_i", "spectre": "lo_i"},
    "lo_q": {"ng": "v(lo_q)", "ads": "lo_q", "spectre": "lo_q"},
    "clk_vco_div": {"ng": "v(clk_vco_div)", "ads": "clk_vco_div",
                    "spectre": "clk_vco_div"},
    "i_vdd": {"ng": "i(vdd)", "ads": "i_vdd", "spectre": "i_vdd"},
}


# ------------------------------------------------------------ parsers
def parse_ngspice_csv(lines: list[str], col: str) -> tuple[list[float], list[float]]:
    """wrdata CSV: columns alternate time,value per signal; header row has names."""
    header = [h.strip() for h in lines[0].strip().split(",")]
    if col not in header:
        raise StageError("parse:ngspice-column", f"{col} not in {header}")
    ti, vi = header.index("time"), header.index(col)
    ts, vs = [], []
    for ln in lines[1:]:
        parts = ln.strip().split(",")
        if len(parts) <= max(ti, vi):
            continue
        try:
            ts.append(float(parts[ti]))
            vs.append(float(parts[vi]))
        except ValueError:
            continue
    return ts, vs


def load_rawread():
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import rawread_ads
    return rawread_ads


def parse_ads_raw(path: Path, want: list[str]) -> dict[str, tuple[list, list]]:
    """canonical->(t, v) via best-effort vector name match in the MDS raw."""
    rr = load_rawread()
    plots = rr.read_raw(str(path))
    plot = plots[0]
    variables = plot.get("vars", [])
    data = plot.get("data", {})
    tvec = None
    for cand in ("time", "V.time", "t"):
        if cand in data:
            tvec = data[cand]
            break
    if tvec is None and variables:
        tvec = data[variables[0]]
    out = {}
    for w in want:
        hit = None
        for v in variables:
            base = v.split(".")[-1].split(":")[-1].lower()
            if base == w.lower() or base == w.lower().replace("_", ""):
                hit = v
                break
        if hit is None:
            raise StageError("parse:ads-vector", f"{w} not in {variables[:20]}")
        out[w] = (list(map(float, tvec)), list(map(float, data[hit])))
    return out


def parse_psfascii(path: Path, want: list[str]) -> dict[str, tuple[list, list]]:
    """psfascii tran reader (Spectre -format psfascii). VALUE holds one block
    per timestep: `( <t> "time" ( <v> "<sig>" ) ... )`, signals in TRACE order."""
    text = path.read_text(errors="replace")
    m = re.search(r"\nVALUE\s*\n(.*)", text, re.S)
    if not m:
        raise StageError("parse:psfascii", "no VALUE section")
    body = m.group(1)
    ts: list[float] = []
    series: dict[str, list[float]] = {}
    for blk in re.finditer(r"\(\s*([-+\d.eE]+)\s+\"time\"\s*(.*?)\n\s*\)",
                           body, re.S):
        ts.append(float(blk.group(1)))
        for val, name in re.findall(r"\(\s*([-+\d.eE]+)\s+\"([^\"]+)\"\s*\)",
                                    blk.group(2)):
            series.setdefault(name, []).append(float(val))
    out = {}
    for w in want:
        if w not in series:
            raise StageError("parse:spectre-vector",
                             f"{w} not in {sorted(series)[:20]}")
        out[w] = (ts, series[w])
    return out


# ------------------------------------------------------------ comparator
def _interp(tq: list[float], t: list[float], v: list[float]) -> list[float]:
    out, j = [], 0
    for q in tq:
        while j < len(t) - 2 and t[j + 1] < q:
            j += 1
        t0, t1 = t[j], t[min(j + 1, len(t) - 1)]
        if t1 == t0:
            out.append(v[j])
            continue
        f = (q - t0) / (t1 - t0)
        out.append(v[j] + f * (v[min(j + 1, len(v) - 1)] - v[j]))
    return out


def compare_waveforms(t_ref, v_ref, t_new, v_new) -> dict:
    """L2-relative difference on the reference grid within overlap."""
    t_lo, t_hi = max(t_ref[0], t_new[0]), min(t_ref[-1], t_new[-1])
    if t_hi <= t_lo:
        raise StageError("compare:overlap", "no time overlap between outputs")
    tq = [x for x in t_ref if t_lo <= x <= t_hi][:: max(1, len(t_ref) // 2000)]
    vr = _interp(tq, t_ref, v_ref)
    vn = _interp(tq, t_new, v_new)
    num = sum((a - b) ** 2 for a, b in zip(vr, vn)) ** 0.5
    den = sum(a ** 2 for a in vr) ** 0.5 or 1e-30
    span = max(max(vr) - min(vr), abs(vr[0])) or 1e-30
    mx = max(abs(a - b) for a, b in zip(vr, vn)) / span
    return {"l2rel": num / den, "maxrel_span": mx, "points": len(tq),
            "window": [t_lo, t_hi]}


# ------------------------------------------------------------ runners
def run_ngspice_reference(tb: Path, timeout: float = 900) -> Path:
    """Run the untouched TB in the container; returns its wrdata CSV path."""
    tb_dir = tb.parent
    rc, out = container_exec(
        f"cd '{tb_dir}' && PDK_ROOT=/foss/pdks "
        "/foss/tools/ngspice/bin/ngspice -b '" + tb.name + "' > /tmp/ng.log 2>&1; "
        "echo RC=$?; tail -5 /tmp/ng.log", timeout=timeout)
    if "RC=0" not in out:
        raise StageError("run:ngspice", out[-400:])
    m = re.search(r"wrdata\s+(\S+)", tb.read_text(), re.I | re.M)
    csv = tb_dir / (m.group(1) if m else tb.stem + "_run.csv")
    if not csv.exists():
        raise StageError("run:ngspice", f"wrdata output missing: {out[-300:]}")
    return csv


def push_and_run_ads(staging: Path, case: str, poll_sec: int = 15,
                     timeout_s: int = 1200) -> dict:
    """Stage dir -> VM, run hpeesofsim detached, poll, pull raw+log."""
    remote_dir = f"{VM_WORKROOT}/{case}"
    tarball = Path("/tmp") / f"{case}_stage.tgz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p in staging.iterdir():
            tf.add(p, arcname=p.name)
    vm_ssh(f"mkdir -p {remote_dir}", check=True)
    cmd = ["sshpass", "-p", _vm_pass(), "ssh", "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-p", "2222",
           "rakesh@127.0.0.1",
           f"cat > {remote_dir}/in.tgz && cd {remote_dir} && "
           "tar xzf in.tgz && rm in.tgz"]
    proc = subprocess.run(cmd, input=tarball.read_bytes(), capture_output=True,
                          timeout=180)
    if proc.returncode != 0:
        raise StageError("transfer:vm-push",
                         (proc.stderr or b"?").decode(errors="replace")[-300:])
    netlist = next(staging.glob("ads_*.net")).name
    vm_ssh(f"cd {remote_dir} && nohup sh -c \"{ADS_ENV} && "
           f"hpeesofsim -n {netlist} -r out.raw > sim.log 2>&1\" "
           f"> /dev/null 2>&1 & echo STARTED", check=True)
    tail, finished = "", False
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(poll_sec)
        _, tail = vm_ssh(f"cd {remote_dir} && tail -4 sim.log 2>/dev/null")
        if "Simulation finished" in tail or "Simulation aborted" in tail \
                or re.search(r"\bERROR\b", tail):
            break
    else:
        raise StageError("run:hpeesofsim", f"timeout after {timeout_s}s: {tail}")
    _, done = vm_ssh(f"cd {remote_dir} && grep -c 'Simulation finished' sim.log || true")
    finished = "1" in done.split()
    if not finished:
        _, why = vm_ssh(f"cd {remote_dir} && grep -i -m6 -e error -e abort sim.log")
        raise StageError("run:hpeesofsim", why or tail)
    outdir = EVIDENCE / "logs"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{case}_ads.out.raw").write_bytes(vm_pull(f"{remote_dir}/out.raw"))
    log = vm_ssh(f"cat {remote_dir}/sim.log")[1] + "\n"
    (outdir / f"{case}_ads.sim.log").write_text(log)
    return {"raw": outdir / f"{case}_ads.out.raw",
            "log": outdir / f"{case}_ads.sim.log"}


# ------------------------------------------------------------ case driver
def run_case_ngspice(tb: Path, signals: list[str]) -> dict:
    csv = run_ngspice_reference(tb)
    lines = csv.read_text().splitlines()
    parsed = {}
    for s in signals:
        ts, vs = parse_ngspice_csv(lines, DEFAULT_SIGNALS[s]["ng"])
        parsed[s] = (ts, vs)
    return parsed


def xcheck_ads(case: str, tb: Path, corner: str, params: dict,
               signals: list[str], tol: dict) -> dict:
    staging = Path("/tmp/xcheck") / f"{case}_ads"
    netlist = adapt(tb, "ads", corner, staging, params)
    artifacts = push_and_run_ads(staging, case)
    ref = run_case_ngspice(tb, signals)
    want = [DEFAULT_SIGNALS[s]["ads"] for s in signals]
    new = parse_ads_raw(artifacts["raw"], want)
    metrics, verdict = {}, True
    l2max = float(tol.get("l2rel", 0.05))
    mxmax = float(tol.get("maxrel_span", 0.10))
    for s in signals:
        m = compare_waveforms(ref[s][0], ref[s][1],
                              new[DEFAULT_SIGNALS[s]["ads"]][0],
                              new[DEFAULT_SIGNALS[s]["ads"]][1])
        ok = m["l2rel"] <= l2max and m["maxrel_span"] <= mxmax
        verdict &= ok
        metrics[s] = {"metrics": m, "ok": bool(ok)}
    return {"case": case, "corner": corner, "params": params,
            "reference": {"tool": "ngspice", "deck": str(tb)},
            "crosscheck": {"tool": "hpeesofsim", "netlist": str(netlist),
                           "log": str(artifacts["log"])},
            "signals": metrics, "verdict": "PASS" if verdict else "FAIL"}


def self_test() -> int:
    """Exercise parsers + comparator with synthetic data (no tools)."""
    t = [i * 1e-12 for i in range(100)]
    a = [((i % 10) - 5) / 5 for i in range(100)]
    b = [x * 1.01 for x in a]
    m = compare_waveforms(t, a, t, b)
    assert m["l2rel"] < 0.02 and m["maxrel_span"] < 0.05, m
    m2 = compare_waveforms(t, a, t, [-x for x in a])
    assert m2["l2rel"] > 1.0

    csv = ("time,v(lo_i),v(lo_q)\n0,0,1\n1e-9,0.5,0.5\n2e-9,1.5,0\n")
    tv, vals = parse_ngspice_csv(csv.splitlines(), "v(lo_i)")
    assert tv == [0.0, 1e-9, 2e-9] and vals == [0.0, 0.5, 1.5]

    psf = """
VALUE
( 0.0 "time"
  ( 0.0 "lo_i" )
  ( 1.5 "clk_vco_div" )
)
( 1.0e-9 "time"
  ( 0.5 "lo_i" )
  ( 1.2 "clk_vco_div" )
)
"""
    tmp = Path("/tmp/xcheck_psf.psf")
    tmp.write_text(psf)
    got = parse_psfascii(tmp, ["lo_i"])
    assert got["lo_i"] == ([0.0, 1e-9], [0.0, 0.5]), got

    print(json.dumps({"self_test": "pass"}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--case")
    ap.add_argument("--tb", type=Path)
    ap.add_argument("--corner", default="tt")
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--signal", action="append", default=None)
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    cfg = json.loads(json.dumps({}))  # placeholder replaced below
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config" / "cases.yaml").read_text())
    case_cfg = cfg["cases"][args.case]
    signals = args.signal or case_cfg.get("signals", list(DEFAULT_SIGNALS))
    params = case_cfg.get("params", {})
    params.update(dict(kv.split("=", 1) for kv in args.param))
    tb = args.tb or LINHT_TB(case_cfg["tb"])

    rep = xcheck_ads(args.case, tb, args.corner, params, signals,
                     cfg.get("transient", {}))
    EVIDENCE.mkdir(exist_ok=True)
    outfile = EVIDENCE / f"{args.case}_{args.corner}_{args.target_default()}.json"
    outfile.write_text(json.dumps(rep, indent=2))
    print(json.dumps({"report": str(outfile), "verdict": rep["verdict"]}))
    return 0 if rep["verdict"] == "PASS" else 1


def LINHT_TB(rel: str) -> Path:
    return REPO_ROOT.parent / "LinHT-rfic" / rel


def _target_default() -> str:
    return "ads"


if __name__ == "__main__":
    sys.exit(main())

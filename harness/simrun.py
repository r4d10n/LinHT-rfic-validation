#!/usr/bin/env python3
"""G003 circuit cross-check harness: one testbench, three simulators.

Reference of record: ngspice running the repo deck unmodified in the
linht_iic container (.control/wrdata CSV parsed directly).
Cross-checks: hpeesofsim on VM samjna (netadapt --target ads deck; default
.ds dataset dumped to text via dsdump on the VM and parsed here).

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
from common import REPO_ROOT, StageError, container_exec, vm_pull, vm_ssh, _vm_pass
from netadapt import adapt

EVIDENCE = REPO_ROOT / "evidence"
VM_WORKROOT = "~/linht_val"

# canonical signal -> per-tool vector names.
# ads: verified against dsdump of probeD/probeJ — nodes by bare name,
# source currents as <InstanceName>.i. ngspice: wrdata column names.
DEFAULT_SIGNALS = {
    "lo_i": {"ng": "v(lo_i)", "ads": "lo_i", "spectre": "lo_i"},
    "lo_q": {"ng": "v(lo_q)", "ads": "lo_q", "spectre": "lo_q"},
    "clk_vco_div": {"ng": "v(clk_vco_div)", "ads": "clk_vco_div",
                    "spectre": "clk_vco_div"},
    "i_vdd": {"ng": "i(vdd)", "ads": "Vdd.i", "spectre": "Vdd:p"},
}


# ------------------------------------------------------------ parsers
def wrdata_signal_order(tb_text: str) -> list[str]:
    """Vector names in wrdata statement order (the CSV has no header)."""
    m = re.search(r"(?:^|\n)\s*wrdata\s+(.+)$", tb_text, re.M | re.I)
    if not m:
        raise StageError("parse:ngspice-wrdata", "no wrdata statement")
    toks = m.group(1).split()
    if toks and toks[0].lower().endswith((".csv", ".txt", ".data")):
        toks = toks[1:]  # leading token is wrdata's output FILE argument
    return toks


def parse_ngspice_csv(lines: list[str], pair_idx: int) -> tuple[list[float], list[float]]:
    """wrdata CSV: whitespace columns alternating (time, value) per signal,
    signals in wrdata-statement order, no header row."""
    ts, vs = [], []
    for ln in lines:
        parts = ln.split()
        c0, c1 = 2 * pair_idx, 2 * pair_idx + 1
        if len(parts) <= c1:
            continue
        try:
            ts.append(float(parts[c0]))
            vs.append(float(parts[c1]))
        except ValueError:
            continue
    return ts, vs


# dsdump declares e.g. `    0: "time" 0 r` plus a flags attribute line
# carrying `indep=yes`; dependents follow per point after the `idx: t` line.
DS_DECL_RE = re.compile(r'^\s*(\d+):\s+"([^"]+)"\s+\d+\s+([a-z])$', re.M)
DS_INDEP_RE = re.compile(r"indep\s*=\s*yes")

def parse_ads_dataset(text: str) -> dict[str, list[float]]:
    """dsdump text. Declarations: `<n>: "name" <type> <r|c>` each followed by
    a flags line; exactly one has `indep=yes` (time). Point rows are
    `idx: <indep value>` then one value line per DEPENDENT, in order."""
    indep, deps = None, []
    for num, name, typ in DS_DECL_RE.findall(text):
        flags_at = text.find(f'"{name}"', 0)
        seg = text[flags_at: flags_at + 200]
        if DS_INDEP_RE.search(seg):
            indep = name
        else:
            deps.append(name)
    m = re.search(r"\* Number of points:\s*(\d+)", text)
    if indep is None and deps:
        # minimal dumps omit the flags block; dsdump always lists time first
        indep = deps.pop(0)
    if not m or not deps or indep is None:
        raise StageError("parse:dsdump", "declarations incomplete")
    npts = int(m.group(1))
    lines = text.split("* Number of points:")[-1].splitlines()
    idx_re = re.compile(r"^(\d+):\s*([-+\d.eE]+)$")
    val_re = re.compile(r"^([-+\d.eENa]+)$")
    times: list[float] = []
    vals: list[list[float]] = [[] for _ in deps]
    i = 0
    while i < len(lines):
        mm = idx_re.match(lines[i].strip())
        if mm:
            times.append(float(mm.group(2)))
            for d in range(len(deps)):
                i += 1
                vm_ = val_re.match(lines[i].strip())
                if not vm_:
                    raise StageError("parse:dsdump",
                                     f"point {mm.group(1)}: expected value, "
                                     f"got {lines[i]!r}")
                vals[d].append(float(vm_.group(1)))
        i += 1
    if len(times) != npts:
        raise StageError("parse:dsdump",
                         f"{len(times)} rows parsed vs {npts} declared")
    out = {"time": times}
    out.update(zip(deps, vals))
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


def compare_waveforms(t_ref, v_ref, t_new, v_new,
                      max_skew: float = 0.0) -> dict:
    """L2-relative difference on the reference grid within overlap.

    With max_skew>0, additionally finds the time shift tau<=max_skew that
    minimises l2rel (cross-simulator edge skew on square waves is expected;
    the aligned figure separates it from genuine amplitude disagreement)."""
    t_lo, t_hi = max(t_ref[0], t_new[0]), min(t_ref[-1], t_new[-1])
    if t_hi <= t_lo:
        raise StageError("compare:overlap", "no time overlap between outputs")
    tq = [x for x in t_ref if t_lo <= x <= t_hi][:: max(1, len(t_ref) // 2000)]
    vr = _interp(tq, t_ref, v_ref)

    def _raw(shift):
        vn = _interp([x + shift for x in tq], t_new, v_new)
        num = sum((a - b) ** 2 for a, b in zip(vr, vn)) ** 0.5
        den = sum(a ** 2 for a in vr) ** 0.5 or 1e-30
        span = max(max(vr) - min(vr), abs(vr[0]), 1e-30)
        mx = max(abs(a - b) for a, b in zip(vr, vn)) / span
        return num / den, mx

    l20, mx0 = _raw(0.0)
    res = {"l2rel": l20, "maxrel_span": mx0, "points": len(tq),
           "window": [t_lo, t_hi], "skew_s": 0.0}
    if max_skew > 0:
        nsteps = 41
        best = (l20, 0.0)
        step = 2 * max_skew / nsteps
        for k in range(-nsteps // 2, nsteps // 2 + 1):
            tau = k * step
            if tau == 0.0:
                continue
            l2t, _ = _raw(tau)
            if l2t < best[0]:
                best = (l2t, tau)
        res["l2rel"] = round(best[0], 6)
        res["skew_s"] = best[1]
    return res

# ------------------------------------------------------------ runners
def _container_path(p: Path) -> str:
    """Host LinHT-rfic path -> container mount (/foss/designs/LinHT_IC)."""
    host_repo = str((REPO_ROOT.parent / "LinHT-rfic").resolve())
    s = str(Path(p).resolve())
    if s.startswith(host_repo):
        return "/foss/designs/LinHT_IC" + s[len(host_repo):]
    return s


def run_ngspice_reference(tb: Path, timeout: float = 900) -> Path:
    """Run the untouched TB in the container; returns local CSV copy.
    Single round trip: simulate, verify wrdata, and cat it back."""
    cdir = _container_path(tb.parent)
    m = re.search(r"(?:^|\n)\s*wrdata\s+(\S+)", tb.read_text(), re.I)
    csv_name = m.group(1) if m else tb.stem + "_run.csv"
    rc, out = container_exec(
        f"cd '{cdir}' && PDK_ROOT=/foss/pdks "
        "/foss/tools/ngspice/bin/ngspice -b '" + tb.name + "' "
        "> /tmp/ng.log 2>&1; RC=$?; echo NGRC=$RC; "
        f"if [ -s '{csv_name}' ]; then echo WRDATA_OK; cat '{csv_name}'; "
        "else tail -20 /tmp/ng.log; fi", timeout=timeout)
    if "NGRC=0" not in out or "WRDATA_OK" not in out:
        # one retry; transient container/profile flakes observed once
        rc, out = container_exec(
            f"cd '{cdir}' && PDK_ROOT=/foss/pdks "
            "/foss/tools/ngspice/bin/ngspice -b '" + tb.name + "' "
            "> /tmp/ng.log 2>&1; RC=$?; echo NGRC=$RC; "
            f"if [ -s '{csv_name}' ]; then echo WRDATA_OK; cat '{csv_name}'; "
            "else tail -20 /tmp/ng.log; ls -la; fi", timeout=timeout)
    if "NGRC=0" not in out or "WRDATA_OK" not in out:
        raise StageError("run:ngspice", out[-400:])
    data = out.split("WRDATA_OK", 1)[1]
    local = Path("/tmp") / f"{tb.stem}_ref.csv"
    local.write_text(data.lstrip("\n") + "\n")
    return local


def push_and_run_ads(staging: Path, case: str, poll_sec: int = 15,
                     timeout_s: int = 1500) -> dict:
    """Stage dir -> VM, hpeesofsim detached, poll, dsdump, pull text+log."""
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
    stem = netlist[len("ads_"):-len(".net")]
    # fire-and-forget: ssh is NEVER waited (a backgrounded remote child can
    # hold the channel open); the poll loop below tracks real progress.
    launcher = subprocess.Popen(
        ["sshpass", "-p", _vm_pass(), "ssh", "-o",
         "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-p", "2222", "rakesh@127.0.0.1",
         f"cd {remote_dir} && chmod +x adsenv.sh && "
         "nohup sh -c '. ./adsenv.sh && "
         f"hpeesofsim -n {netlist} > sim.log 2>&1 && "
         f"dsdump {stem}.ds > dataset.txt' "
         "</dev/null > /dev/null 2>&1 & echo STARTED"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL)
    time.sleep(3)
    tail, done = "", False
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(poll_sec)
        _, tail = vm_ssh(
            f"cd {remote_dir} && "
            "if [ -s dataset.txt ] && grep -q 'Simulation finished' sim.log; "
            "then echo DONE_OK; fi; "
            "grep -m1 'terminated due to an error' sim.log || true")
        if "DONE_OK" in tail:
            done = True
            break
        if "terminated due to an error" in tail:
            _, why = vm_ssh(f"cd {remote_dir} && grep -i -B2 -A4 -m2 error sim.log")
            raise StageError("run:hpeesofsim", why or tail)
    if not done:
        raise StageError("run:hpeesofsim", f"incomplete: {tail}")
    outdir = EVIDENCE / "logs"
    outdir.mkdir(parents=True, exist_ok=True)
    dataset = vm_pull(f"{remote_dir}/dataset.txt").decode(errors="replace")
    (outdir / f"{case}_ads.dataset.txt").write_text(dataset)
    log = vm_ssh(f"cat {remote_dir}/sim.log")[1] + "\n"
    (outdir / f"{case}_ads.sim.log").write_text(log)
    return {"dataset": outdir / f"{case}_ads.dataset.txt",
            "log": outdir / f"{case}_ads.sim.log"}


# ------------------------------------------------------------ case driver
def xcheck_ads(case: str, tb: Path, corner: str, params: dict,
               signals: list[str], tol: dict) -> dict:
    staging = Path("/tmp/xcheck") / f"{case}_ads"
    adapt(tb, "ads", corner, staging, params)
    artifacts = push_and_run_ads(staging, case)

    csv = run_ngspice_reference(tb)
    lines = csv.read_text().splitlines()
    order = wrdata_signal_order(tb.read_text())
    ref = {}
    for s in signals:
        ngname = DEFAULT_SIGNALS[s]["ng"]
        if ngname not in order:
            raise StageError("parse:ngspice-column",
                             f"{ngname} not in wrdata order {order}")
        ref[s] = parse_ngspice_csv(lines, order.index(ngname))

    data = parse_ads_dataset(Path(artifacts["dataset"]).read_text())
    l2max = float(tol.get("l2rel", 0.05))
    mxmax = float(tol.get("maxrel_span", 0.10))
    metrics, verdict = {}, True
    for s in signals:
        ads_name = DEFAULT_SIGNALS[s]["ads"]
        if ads_name not in data:
            raise StageError("parse:ads-vector",
                             f"{ads_name} not in dataset vars {list(data)}")
        new = (data["time"], data[ads_name])
        m = compare_waveforms(ref[s][0], ref[s][1], new[0], new[1],
                              max_skew=float(tol.get("max_skew_s", 0)))
        ok = (m["l2rel"] <= l2max
              and abs(m.get("skew_s", 0.0)) <= float(tol.get("max_skew_s", 0)))
        verdict &= ok
        metrics[s] = {"metrics": m, "ok": bool(ok)}
    return {"case": case, "corner": corner, "params": params,
            "reference": {"tool": "ngspice", "deck": str(tb)},
            "crosscheck": {"tool": "hpeesofsim",
                           "log": str(artifacts["log"])},
            "signals": metrics, "verdict": "PASS" if verdict else "FAIL"}


# ------------------------------------------------------------ self test
DS_FIXTURE = '''* Vectorset name: "Tran1.TRAN"
    0: "time" 0 r
    1: "lo_i" 0 r
    2: "Vdd.i" 0 r
----------
* Number of points: 2
0: 0
0
0.001
1
1: 5e-11
1
0.002
2'''


def self_test() -> int:
    """Exercise parsers + comparator with synthetic data (no tools)."""
    t = [i * 1e-12 for i in range(100)]
    a = [((i % 10) - 5) / 5 for i in range(100)]
    b = [x * 1.01 for x in a]
    m = compare_waveforms(t, a, t, b)
    assert m["l2rel"] < 0.02 and m["maxrel_span"] < 0.05, m
    m2 = compare_waveforms(t, a, t, [-x for x in a])
    assert m2["l2rel"] > 1.0

    csv = ("0 0 0 1.5\n"
           "5e-10 0.25 0 1.5\n"
           "1e-9 0.5 0 1.5\n")
    tv, vals = parse_ngspice_csv(csv.splitlines(), 0)
    assert tv == [0.0, 5e-10, 1e-9] and vals == [0.0, 0.25, 0.5], (tv, vals)
    tq, vq = parse_ngspice_csv(csv.splitlines(), 1)
    assert vq == [1.5, 1.5, 1.5]

    got = parse_ads_dataset(DS_FIXTURE)
    assert got["time"] == [0.0, 5e-11], got
    assert got["lo_i"] == [0.0, 1.0] and got["Vdd.i"] == [0.001, 0.002]

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

    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "config" / "cases.yaml").read_text())
    case_cfg = cfg["cases"][args.case]
    signals = args.signal or case_cfg.get("signals", list(DEFAULT_SIGNALS))
    params = dict(case_cfg.get("params", {}))
    params.update(dict(kv.split("=", 1) for kv in args.param))
    tb = args.tb or (REPO_ROOT.parent / "LinHT-rfic" / case_cfg["tb"])

    rep = xcheck_ads(args.case, tb, args.corner, params, signals,
                     cfg.get("transient", {}))
    EVIDENCE.mkdir(exist_ok=True)
    outfile = EVIDENCE / f"{args.case}_{args.corner}_ads_report.json"
    outfile.write_text(json.dumps(rep, indent=2))
    print(json.dumps({"report": str(outfile), "verdict": rep["verdict"]}))
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

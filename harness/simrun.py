#!/usr/bin/env python3
"""G003 circuit cross-check harness: one testbench, three simulators.

Reference of record: ngspice running the repo deck unmodified in the
linht_iic container (wrdata CSV). Cross-check: hpeesofsim on VM samjna
(netadapt --target ads deck; .ds extracted VM-side).

Metric families (config/cases.yaml -> transient):
- digital signals (logic outputs): rising-edge count equality + per-edge
  skew bounds + logic-swing match. Pointwise L2 is meaningless on square
  waves when device-model differences accumulate ~20 ps/edge of phase.
- current rails (i_*): moving-average smoothed L2 (spike positions shift
  sub-ns between simulators).
Stages are named adapt/push/run/parse/compare per AGENTS.md contract.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import time
from collections import deque
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO_ROOT, StageError, container_exec, vm_pull, vm_ssh, _vm_pass
from netadapt import adapt

EVIDENCE = REPO_ROOT / "evidence"
VM_WORKROOT = "~/linht_val"

# canonical signal -> per-tool vector names.
# ads: verified against dsdump of probeD/probeJ — nodes by bare name,
# source currents as <InstanceName>.i. ngspice: wrdata vector names.
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


DS_DECL_RE = re.compile(r'^\s*(\d+):\s+"([^"]+)"\s+\d+\s+([a-z])$', re.M)
DS_INDEP_RE = re.compile(r"indep\s*=\s*yes")


def parse_ads_dataset(text: str) -> dict[str, list[float]]:
    """dsdump text. Declarations `<n>: "name" <t> <r|c>` (time carries
    indep=yes); point rows `idx: <time>` then one value per dependent."""
    indep, deps = None, []
    for num, name, typ in DS_DECL_RE.findall(text):
        flags_at = text.find(f'"{name}"')
        if DS_INDEP_RE.search(text[flags_at:flags_at + 200]):
            indep = name
        else:
            deps.append(name)
    m = re.search(r"\* Number of points:\s*(\d+)", text)
    if indep is None and deps:
        indep = deps.pop(0)  # minimal dumps omit flags; time is listed first
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


def parse_psfascii(path: Path, want: list[str]) -> dict[str, list[float]]:
    """Real Spectre psfascii tran layout (verified on samjna output):
    TRACE name/unit declarations, then a VALUE section of rows
    `"name" value` — one row per signal per timestep, time included as
    `"time" <t>`. Returns dict incl. "time". Only `want` (+time) kept."""
    ts: list[float] = []
    tracked = [w for w in want if w != "time"]
    cols: dict[str, list[float]] = {w: [] for w in tracked}
    pend: dict[str, float] = {}
    cur = None
    n = 0
    need = len(tracked)
    in_val = False
    for ln in path.open(errors="replace"):
        if not in_val:
            if ln.startswith("VALUE"):
                in_val = True
            continue
        parts = ln.split('"')
        if len(parts) >= 3:
            name = parts[1]
            try:
                fv = float(parts[2])
            except ValueError:
                continue
            if name == "time":
                if cur is not None and n == need:
                    ts.append(cur)
                    for w in tracked:
                        cols[w].append(pend[w])
                cur = fv
                n = 0
            elif name in cols:
                pend[name] = fv
                n += 1
    if cur is not None and n == need:
        ts.append(cur)
        for w in tracked:
            cols[w].append(pend[w])
    if not ts:
        raise StageError("parse:psfascii", "no rows parsed")
    out = {"time": ts}
    out.update(cols)
    return out


# ------------------------------------------------------------ comparators
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
    """Pointwise (optionally best-shift) L2 comparison on the ref grid."""
    t_lo = max(t_ref[0], t_new[0])
    t_hi = min(t_ref[-1], t_new[-1])
    if t_hi <= t_lo:
        raise StageError("compare:overlap", "no time overlap between outputs")
    tq = [x for x in t_ref if t_lo <= x <= t_hi][:: max(1, len(t_ref) // 2000)]
    vr = _interp(tq, t_ref, v_ref)

    def raw(shift):
        vn = _interp([x + shift for x in tq], t_new, v_new)
        num = sum((a - b) ** 2 for a, b in zip(vr, vn)) ** 0.5
        den = sum(a ** 2 for a in vr) ** 0.5 or 1e-30
        span = max(max(vr) - min(vr), abs(vr[0]), 1e-30)
        mx = max(abs(a - b) for a, b in zip(vr, vn)) / span
        return num / den, mx

    l20, mx0 = raw(0.0)
    res = {"l2rel": l20, "maxrel_span": mx0, "skew_s": 0.0}
    if max_skew > 0:
        best = (l20, 0.0)
        nsteps, step = 41, 2 * max_skew / 41
        for k in range(-nsteps // 2, nsteps // 2 + 1):
            tau = k * step
            if tau == 0.0:
                continue
            l2t, _ = raw(tau)
            if l2t < best[0]:
                best = (l2t, tau)
        res["l2rel"], res["skew_s"] = round(best[0], 6), best[1]
    return res


def _crossings(t: list[float], v: list[float], vth: float) -> list[float]:
    out = []
    for i in range(1, len(v)):
        a, b = v[i - 1], v[i]
        if b != a and ((a < vth <= b) or (a > vth >= b)):
            out.append(t[i - 1] + (vth - a) * (t[i] - t[i - 1]) / (b - a))
    return out


def _movavg(v: list[float], w: int) -> list[float]:
    if w <= 1:
        return v[:]
    out, acc, q = [], 0.0, deque()
    for x in v:
        q.append(x)
        acc += x
        if len(q) > w:
            acc -= q.popleft()
        out.append(acc / len(q))
    return out


def compare_digital(t_ref, v_ref, t_new, v_new, vth: float,
                    max_median_skew: float, max_edge_skew: float,
                    amp_tol: float = 0.05) -> dict:
    """Edge-count equality + per-edge skew bounds + swing match."""
    er = [e for e in _crossings(t_ref, v_ref, vth)]
    en = [e for e in _crossings(t_new, v_new, vth)]
    lo = max(min(er) if er else 0, min(en) if en else 0)
    hi = min(max(t_ref), max(t_new))
    er_w = [e for e in er if lo <= e <= hi]
    en_w = [e for e in en if lo <= e <= hi]
    skews = [min(abs(e - x) for x in en_w) if en_w else float("inf")
             for e in er_w]
    med = sorted(skews)[len(skews) // 2] if skews else float("inf")
    mx = max(skews) if skews else float("inf")
    span_r = max(v_ref) - min(v_ref)
    span_n = max(v_new) - min(v_new)
    amp_ok = abs(span_r - span_n) <= amp_tol * max(span_r, 1e-9)
    count_ok = len(er_w) == len(en_w)
    ok = count_ok and med <= max_median_skew and mx <= max_edge_skew and amp_ok
    return {"edges_ref": len(er_w), "edges_new": len(en_w),
            "count_ok": bool(count_ok), "median_skew_s": med,
            "max_skew_s": mx, "amp_ok": bool(amp_ok), "ok": bool(ok)}


def compare_smoothed(t_ref, v_ref, t_new, v_new, bin_s: float,
                     l2_tol: float) -> dict:
    """Smoothed L2 (for switching currents whose spikes shift sub-ns)."""
    dt = max(t_ref[1] - t_ref[0], 1e-15)
    w = max(3, int(bin_s / dt) | 1)
    m = compare_waveforms(t_ref, _movavg(v_ref, w), t_new, _movavg(v_new, w))
    den = sum(abs(x) for x in _movavg(v_ref, w)) / max(len(v_ref), 1) or 1e-30
    tq = [x for x in t_ref if max(t_new[0], t_ref[0]) <= x <= min(t_ref[-1], t_new[-1])]
    vn = _interp(tq, t_new, _movavg(v_new, w))
    vr = _interp(tq, t_ref, _movavg(v_ref, w))
    l2 = sum((a - b) ** 2 for a, b in zip(vr, vn)) ** 0.5 / (den * len(tq) ** 0.5)
    ok = bool(l2 <= l2_tol)
    return {"l2rel": round(l2, 6), "maxrel_span": m["maxrel_span"], "ok": ok}



def compare_supply(t_ref, v_ref, t_new, v_new, t_ss_frac: float = 0.33,
                   mean_tol: float = 0.02, env_tol: float = 0.25) -> dict:
    """Supply-rail agreement: steady-state MEAN current (the CACE spec
    quantity) within mean_tol; peak envelope within env_tol. Spike-by-spike
    alignment is explicitly not required."""
    t0_r = t_ref[0] + (t_ref[-1] - t_ref[0]) * t_ss_frac
    t0_n = t_new[0] + (t_new[-1] - t_new[0]) * t_ss_frac
    mr = statistics.fmean(y for x, y in zip(t_ref, v_ref) if x >= t0_r)
    mn = statistics.fmean(y for x, y in zip(t_new, v_new) if x >= t0_n)
    mean_rel = abs(mr - mn) / max(abs(mr), 1e-12)
    env_r = (min(v_ref), max(v_ref))
    env_n = (min(v_new), max(v_new))
    env_dev = max(abs(a - b) for a, b in zip(env_r, env_n)) \
        / max(abs(env_r[1] - env_r[0]), 1e-12)
    ok = bool(mean_rel <= mean_tol and env_dev <= env_tol)
    return {"mean_ref_a": mr, "mean_new_a": mn, "mean_rel": round(mean_rel, 6),
            "envelope_dev": round(env_dev, 4), "ok": ok}

def compare_digital(t_ref, v_ref, t_new, v_new, vth: float,
                    max_median_skew: float, max_edge_skew: float,
                    amp_tol: float = 0.05,
                    edge_margin_s: float = 1e-9) -> dict:
    """Edge-count equality (interior window; +/-1 at boundaries allowed)
    + per-edge skew bounds + swing match."""
    er_all = _crossings(t_ref, v_ref, vth)
    en_all = _crossings(t_new, v_new, vth)
    lo = max(min(t_ref), min(t_new)) + edge_margin_s
    hi = min(max(t_ref), max(t_new)) - edge_margin_s
    er_w = [e for e in er_all if lo <= e <= hi]
    en_w = [e for e in en_all if lo <= e <= hi]
    n_r, n_n = len(er_w), len(en_w)
    # match each reference edge to its nearest new edge
    skews = [min(abs(e - x) for x in en_w) if en_w else float("inf")
             for e in er_w]
    med = sorted(skews)[len(skews) // 2] if skews else float("inf")
    mx = max(skews) if skews else float("inf")
    span_r = max(v_ref) - min(v_ref)
    span_n = max(v_new) - min(v_new)
    amp_ok = abs(span_r - span_n) <= amp_tol * max(span_r, 1e-9)
    count_ok = abs(n_r - n_n) <= 1
    ok = count_ok and med <= max_median_skew and mx <= max_edge_skew and amp_ok
    return {"edges_ref": n_r, "edges_new": n_n,
            "count_ok": bool(count_ok), "median_skew_s": med,
            "max_skew_s": mx, "amp_ok": bool(amp_ok), "ok": bool(ok)}


# ------------------------------------------------------------ runners
CONTAINER_REPO = "/foss/designs/LinHT-rfic"  # recreated 2026-08-26 mount


def _container_path(p: Path) -> str:
    host_repo = str((REPO_ROOT.parent / "LinHT-rfic").resolve())
    s = str(Path(p).resolve())
    return CONTAINER_REPO + s[len(host_repo):] \
        if s.startswith(host_repo) else s


def run_ngspice_reference(tb: Path, timeout: float = 900) -> Path:
    """Untouched TB in the container; single round trip returns local CSV."""
    cdir = _container_path(tb.parent)
    m = re.search(r"(?:^|\n)\s*wrdata\s+(\S+)", tb.read_text(), re.I)
    csv_name = m.group(1) if m else tb.stem + "_run.csv"
    cmd = (f"cd '{cdir}' && PDK_ROOT=/foss/pdks "
           "/foss/tools/ngspice/bin/ngspice -b '" + tb.name + "' "
           "> /tmp/ng.log 2>&1; RC=$?; echo NGRC=$RC; "
           f"if [ -s '{csv_name}' ]; then echo WRDATA_OK; cat '{csv_name}'; "
           "else tail -20 /tmp/ng.log; ls -la; fi")
    rc, out = container_exec(cmd, timeout=timeout)
    if "NGRC=0" not in out or "WRDATA_OK" not in out:
        rc, out = container_exec(cmd, timeout=timeout)  # one retry (flaked once)
    if "NGRC=0" not in out or "WRDATA_OK" not in out:
        raise StageError("run:ngspice", out[-400:])
    data = out.split("WRDATA_OK", 1)[1]
    local = Path("/tmp") / f"{tb.stem}_ref.csv"
    local.write_text(data.lstrip("\n") + "\n")
    return local


def push_and_run_ads(staging: Path, case: str, poll_sec: int = 15,
                     timeout_s: int = 1500) -> dict:
    """Stage dir -> VM; hpeesofsim detached via fire-and-forget ssh; poll;
    dsdump (retry once — crashed once on a 45 MB dataset); pull text+log."""
    remote_dir = f"{VM_WORKROOT}/{case}"
    tarball = Path("/tmp") / f"{case}_stage.tgz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p in staging.iterdir():
            tf.add(p, arcname=p.name)
    vm_ssh(f"mkdir -p {remote_dir}", check=True)
    cmd = ["sshpass", "-p", _vm_pass(), "ssh", "-o",
           "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-p", "2222", "rakesh@127.0.0.1",
           f"cat > {remote_dir}/in.tgz && cd {remote_dir} && "
           "tar xzf in.tgz && rm in.tgz"]
    proc = subprocess.run(cmd, input=tarball.read_bytes(), capture_output=True,
                          timeout=180)
    if proc.returncode != 0:
        raise StageError("transfer:vm-push",
                         (proc.stderr or b"?").decode(errors="replace")[-300:])
    netlist = next(staging.glob("ads_*.net")).name
    stem = netlist[len("ads_"):-len(".net")]
    # fire-and-forget launch: never waited (backgrounded remote child can
    # hold the channel open); the poll loop tracks real progress.
    subprocess.Popen(
        ["sshpass", "-p", _vm_pass(), "ssh", "-o",
         "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-p", "2222", "rakesh@127.0.0.1",
         f"cd {remote_dir} && chmod +x adsenv.sh && "
         "nohup sh -c '. ./adsenv.sh && "
         f"hpeesofsim -n {netlist} > sim.log 2>&1 && "
         "dsdump linht_xcheck.ds > dataset.txt && "
         "echo DSDUMP_DONE >> dataset.txt' "
         "</dev/null > /dev/null 2>&1 & echo STARTED"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL)
    time.sleep(5)
    tail, done = "", False
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(poll_sec)
        _, tail = vm_ssh(
            f"cd {remote_dir} && "
            "if grep -q DSDUMP_DONE dataset.txt 2>/dev/null && grep -q 'Simulation finished' sim.log; "
            "then echo DONE_OK; fi; "
            "grep -m1 'terminated due to an error\\|crash report' sim.log || true")
        if "DONE_OK" in tail:
            done = True
            break
        if "terminated due to an error" in tail or "crash report" in tail:
            _, why = vm_ssh(f"cd {remote_dir} && grep -i -B2 -A4 -m2 error sim.log")
            raise StageError("run:hpeesofsim", why or tail)
        if "-s dataset.txt" in tail and "DONE_OK" not in tail and \
                time.time() - t0 > 300:
            pass  # keep polling; dsdump may take tens of seconds
    if not done:
        raise StageError("run:hpeesofsim", f"incomplete after {timeout_s}s: {tail}")
    outdir = EVIDENCE / "logs"
    outdir.mkdir(parents=True, exist_ok=True)
    dataset = vm_pull(f"{remote_dir}/dataset.txt").decode(errors="replace")
    (outdir / f"{case}_ads.dataset.txt").write_text(dataset)
    log = vm_ssh(f"cat {remote_dir}/sim.log")[1] + "\n"
    (outdir / f"{case}_ads.sim.log").write_text(log)
    return {"dataset": outdir / f"{case}_ads.dataset.txt",
            "log": outdir / f"{case}_ads.sim.log"}


# ------------------------------------------------------------ case driver
def push_and_run_spectre(staging: Path, case: str, poll_sec: int = 10,
                         timeout_s: int = 1200) -> dict:
    """Stage dir -> VM; spectre detached; psfascii output pulled back."""
    remote_dir = f"{VM_WORKROOT}/{case}_spc"
    tarball = Path("/tmp") / f"{case}_spc_stage.tgz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p in staging.iterdir():
            tf.add(p, arcname=p.name)
    vm_ssh(f"rm -rf {remote_dir} && mkdir -p {remote_dir}", check=True)
    cmd = ["sshpass", "-p", _vm_pass(), "ssh", "-o",
           "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-p", "2222", "rakesh@127.0.0.1",
           f"cat > {remote_dir}/in.tgz && cd {remote_dir} && "
           "tar xzf in.tgz && rm in.tgz && "
           "cp ~/linht/ads/pdk/va/*.include . 2>/dev/null; true"]
    proc = subprocess.run(cmd, input=tarball.read_bytes(), capture_output=True,
                          timeout=180)
    if proc.returncode != 0:
        raise StageError("transfer:vm-push",
                         (proc.stderr or b"?").decode(errors="replace")[-300:])
    netlist = next(staging.glob("spc_*.net")).name
    subprocess.Popen(
        ["sshpass", "-p", _vm_pass(), "ssh", "-o",
         "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-p", "2222", "rakesh@127.0.0.1",
         f"cd {remote_dir} && export PATH=/opt/cadence/installs/SPECTRE251/bin:$PATH && "
         "nohup sh -c 'spectre " + netlist +
         " -format psfascii -raw psf > sim.log 2>&1' "
         "</dev/null > /dev/null 2>&1 & echo GO"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL)
    time.sleep(5)
    t0 = time.time()
    tail = ""
    while time.time() - t0 < timeout_s:
        time.sleep(poll_sec)
        _, tail = vm_ssh(
            f"cd {remote_dir} && ls psf/tran*.tran* >/dev/null 2>&1 && "
            "grep -q 'completes with' sim.log && echo SPC_DONE || true")
        if "SPC_DONE" in tail:
            break
        _, errs = vm_ssh(f"cd {remote_dir} && grep -c ERROR sim.log || true")
        if errs.strip().isdigit() and int(errs) > 0 and \
                time.time() - t0 > 60:
            raise StageError("run:spectre",
                             vm_ssh(f"cd {remote_dir} && grep -m3 -A3 ERROR sim.log")[1])
    else:
        raise StageError("run:spectre", f"incomplete: {tail}")
    outdir = EVIDENCE / "logs"
    outdir.mkdir(parents=True, exist_ok=True)
    psf_name = next(n for n in vm_ssh(f"cd {remote_dir} && ls psf/")[1].split()
                    if n.startswith("tran"))
    blob = vm_pull(f"{remote_dir}/psf/{psf_name}")
    (outdir / f"{case}_spectre.psfascii").write_bytes(blob)
    log = vm_ssh(f"cat {remote_dir}/sim.log")[1] + "\n"
    (outdir / f"{case}_spectre.sim.log").write_text(log)
    return {"psf": outdir / f"{case}_spectre.psfascii",
            "log": outdir / f"{case}_spectre.sim.log"}


def xcheck_ads(case: str, tb: Path, corner: str, params: dict,
               signals: list[str], tol: dict) -> dict:
    staging = Path("/tmp/xcheck") / f"{case}_ads"
    adapt(tb, "ads", corner, staging, params)
    artifacts = push_and_run_ads(staging, case)

    csv = run_ngspice_reference(tb)
    lines = csv.read_text().splitlines()
    order = wrdata_signal_order(tb.read_text())
    data = parse_ads_dataset(Path(artifacts["dataset"]).read_text())

    dig = tol.get("digital", {})
    sup = tol.get("supply", {})
    metrics, verdict = {}, True
    def ref_pair(s):
        return parse_ngspice_csv(lines, order.index(DEFAULT_SIGNALS[s]["ng"]))

    for s in signals:
        ads_name = DEFAULT_SIGNALS[s]["ads"]
        if ads_name not in data:
            raise StageError("parse:ads-vector",
                             f"{ads_name} not in dataset vars {list(data)}")
        new = (data["time"], data[ads_name])
        if s.startswith("i_"):
            m = compare_supply(*ref_pair(s), new[0], new[1],
                               mean_tol=float(sup.get("mean_tol", 0.02)),
                               env_tol=float(sup.get("env_tol", 0.25)))
            kind = "supply"
        else:
            m = compare_digital(*ref_pair(s), new[0], new[1],
                                vth=float(dig.get("vth_v", 0.75)),
                                max_median_skew=float(dig.get("median_skew_s", 1e-10)),
                                max_edge_skew=float(dig.get("max_skew_s", 5e-10)))
            kind = "edges"
        ok = bool(m.get("ok"))
        verdict &= ok
        metrics[s] = {"kind": kind, "metrics": m, "ok": ok}

    return {"case": case, "corner": corner, "params": params,
            "reference": {"tool": "ngspice", "deck": str(tb)},
            "crosscheck": {"tool": "hpeesofsim",
                           "log": str(artifacts["log"])},
            "signals": metrics, "verdict": "PASS" if verdict else "FAIL"}


def xcheck_spectre(case: str, tb: Path, corner: str, params: dict,
                   signals: list[str], tol: dict) -> dict:
    staging = Path("/tmp/xcheck") / f"{case}_spc"
    adapt(tb, "spectre", corner, staging, params)
    artifacts = push_and_run_spectre(staging, case)

    csv = run_ngspice_reference(tb)
    lines = csv.read_text().splitlines()
    order = wrdata_signal_order(tb.read_text())
    data_ps = Path(artifacts["psf"])
    dig = tol.get("digital", {})
    sup = tol.get("supply", {})
    metrics, verdict = {}, True
    for s in signals:
        spec_name = DEFAULT_SIGNALS[s]["spectre"]
        ref = parse_ngspice_csv(lines, order.index(DEFAULT_SIGNALS[s]["ng"]))
        parsed = parse_psfascii(data_ps, [spec_name])
        new = (parsed["time"], parsed[spec_name])
        if s.startswith("i_"):
            m = compare_supply(*ref, new[0], new[1],
                               mean_tol=float(sup.get("mean_tol", 0.02)),
                               env_tol=float(sup.get("env_tol", 0.25)))
            kind = "supply"
        else:
            m = compare_digital(*ref, new[0], new[1],
                                vth=float(dig.get("vth_v", 0.75)),
                                max_median_skew=float(dig.get("median_skew_s", 1e-10)),
                                max_edge_skew=float(dig.get("max_skew_s", 5e-10)))
            kind = "edges"
        ok = bool(m.get("ok"))
        verdict &= ok
        metrics[s] = {"kind": kind, "metrics": m, "ok": ok}
    return {"case": case, "corner": corner, "params": params,
            "reference": {"tool": "ngspice", "deck": str(tb)},
            "crosscheck": {"tool": "spectre",
                           "log": str(artifacts["log"])},
            "signals": metrics, "verdict": "PASS" if verdict else "FAIL"}


# ------------------------------------------------------------ self test
DS_FIXTURE = '''* Vectorset name: "Tran1.TRAN"
    0: "time" 0 r
	number of attributes: 1
	    "flags" = "time type=real indep=yes"
    1: "lo_i" 0 r
	number of attributes: 1
	    "flags" = "voltage type=real indep=no"
----------
* Number of points: 3
0: 0
0
1: 1e-9
0.5
2: 2e-9
1.5'''


def self_test() -> int:
    t = [i * 1e-12 for i in range(100)]
    a = [((i % 10) - 5) / 5 for i in range(100)]
    b = [x * 1.01 for x in a]
    m = compare_waveforms(t, a, t, b, max_skew=0)
    assert m["l2rel"] < 0.02, m

    # square wave with 50ps edge skew -> aligned comparator must find it
    sq_t = [i * 1e-12 for i in range(4000)]
    sq = [1.5 if (i // 500) % 2 == 0 else 0.0 for i in range(4000)]
    shifted = [1.5 if ((i - 50) // 500) % 2 == 0 else 0.0 for i in range(4000)]
    d = compare_digital(sq_t, sq, sq_t, shifted, vth=0.75,
                        max_median_skew=1e-10, max_edge_skew=5e-10)
    assert d["count_ok"] and d["median_skew_s"] <= 1e-10 and d["ok"], d

    psf = """TRACE
"lo_i" "V"
VALUE
"time" 0.000000000000000e+00
"lo_i" 0.000000000000000e+00
"time" 1.000000000000000e-09
"lo_i" 5.000000000000000e-01
"""
    tmp = Path("/tmp/xcheck_psf.psf")
    tmp.write_text(psf)
    gotp = parse_psfascii(tmp, ["lo_i"])
    assert gotp["time"] == [0.0, 1e-9] and gotp["lo_i"] == [0.0, 0.5], gotp

    got = parse_ads_dataset(DS_FIXTURE)
    assert got["time"] == [0.0, 1e-9, 2e-9], got
    assert got["lo_i"] == [0.0, 0.5, 1.5], got

    csv = ("0 0 0 1.5\n5e-10 0.25 0 1.5\n1e-9 0.5 0 1.5\n")
    tv, vals = parse_ngspice_csv(csv.splitlines(), 0)
    assert tv == [0.0, 5e-10, 1e-9] and vals == [0.0, 0.25, 0.5]

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
    ap.add_argument("--target", choices=["ads", "spectre"], default="ads")
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

    if args.target == "ads":
        rep = xcheck_ads(args.case, tb, args.corner, params, signals,
                         cfg.get("transient", {}))
    else:
        rep = xcheck_spectre(args.case, tb, args.corner, params, signals,
                             cfg.get("transient", {}))
    EVIDENCE.mkdir(exist_ok=True)
    outfile = EVIDENCE / f"{args.case}_{args.corner}_{args.target}_report.json"
    outfile.write_text(json.dumps(rep, indent=2))
    print(json.dumps({"report": str(outfile), "verdict": rep["verdict"]}))
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

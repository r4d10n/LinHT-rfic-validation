#!/usr/bin/env python3
"""G002 Siemens-Calibre BEOL DRC runner for the IHP SG13G2 PDK.

Stages a gunzipped copy of the layout GDS, pushes deck+layout to the VM,
runs `calibre -drc -hier` detached (nohup) with license discovery, polls,
pulls log/summary/results and parses them into an evidence JSON.

Usage:
  harness/pvrun.py --self-test
  harness/pvrun.py --deck calibre/sg13g2_beol.drc \
                   --gds layout/chip_top_logo_fill.gds.gz [--out evidence/wave1_drc]
                   [--timeout S] [--keep-rdb]

Exit 0 iff calibre ran AND results were parsed. A non-zero violation count is
data, not failure (AGENTS.md §0: parsed metrics decide).

Stage names used in errors:
  stage-gds | transfer:vm-push | transfer:vm-pull | run:calibre |
  run:calibre-license | parse:calibre | compare:magic | access:vm-pass
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import REPO_ROOT, StageError, vm_pull, vm_push, vm_ssh

CALIBRE_BIN = "/opt/siemens/calibre/bin/calibre"
VM_RUNDIR = "pv_wave1"          # relative to $HOME on the VM
DEFAULT_OUT = "evidence/wave1_drc"
DEFAULT_TIMEOUT = 7200          # seconds; chip is ~50M polygons
POLL_INTERVAL = 20              # short ssh polls only — never hold foreground

# License discovery: first candidate that produces a completed run wins.
# The winning variable is recorded in the report + docs/calibre_deck_notes.md.
LICENSE_CANDIDATES = [
    "MGLS_LICENSE_FILE=1717@10.180.60.101",
    "LM_LICENSE_FILE=1717@10.180.60.101",
    "MGLS_LICENSE_FILE=1717@siemens-dli",   # unlikely; documents hostname form
]


# Magic baseline report format (LinHT-rfic/verification/drc/*.magic.drc.rpt):
#   chip_top_logo_fill
#   ----------------------------------------
#   [INFO] COUNT: 0
MAGIC_COUNT_RE = re.compile(r"COUNT:\s*(\d+)")

# Summary-report rule lines look like (v2026.3_27.19):
#   RULECHECK M1_a ....... TOTAL Result Count = 12 (12)
RULECHECK_RE = re.compile(
    r"^\s*RULECHECK\s+(\S+)\s+\.+\s+TOTAL Result Count\s*=\s*(\d+)",
    re.MULTILINE)


# ---------------------------------------------------------------------------
# pure helpers (exercised by --self-test)
def rule_names_from_deck(deck_text: str) -> list[str]:
    """Ordered list of check names from brace-named RuleCheck statements:
    Name { INT|EXT|ENC|AREA ... } — verified SVRF dialect of Calibre 2026.3."""
    names = []
    for m in re.finditer(
            r"(?m)^([A-Za-z][A-Za-z0-9_]*)\s*\{\s*"
            r"(INT|EXT|SPACE|ENC|AREA|OVERLAP|OUTSIDE|INSIDE)\b",
            deck_text):
        name = m.group(1)
        if name not in names:
            names.append(name)
    return names


def expand_runset(template_text: str, **tokens: str) -> str:
    """Substitute @TOKEN@ placeholders (we expand templates ourselves)."""
    out = template_text
    for key, val in tokens.items():
        out = out.replace(f"@{key.upper()}@", val)
    leftover = re.findall(r"@[A-Z_]+@", out)
    if leftover:
        raise StageError("expand:runset", f"unexpanded tokens: {leftover}")
    return out


def rewrite_layout(deck_text: str, gds_name: str, primary: str) -> str:
    """Rewrite LAYOUT PATH / LAYOUT PRIMARY lines in the deck.

    Calibre expands $VAR in LAYOUT PATH but NOT in LAYOUT PRIMARY, so both are
    made concrete here before the deck ever reaches the VM.
    """
    deck_text, n_path = re.subn(
        r'(?m)^LAYOUT PATH\s+.*$', f'LAYOUT PATH       "{gds_name}"', deck_text)
    deck_text, n_prim = re.subn(
        r'(?m)^LAYOUT PRIMARY\s+.*$', f'LAYOUT PRIMARY    "{primary}"',
        deck_text)
    if n_path != 1 or n_prim != 1:
        raise StageError("stage-gds",
                         f"deck must have exactly one LAYOUT PATH and one "
                         f"LAYOUT PRIMARY line (found {n_path}/{n_prim})")
    return deck_text

def parse_summary(text: str) -> dict[str, int]:
    """Parse per-rule counts from a Calibre DRC summary report.
    Real line shape (v2026.3_27.19):
      RULECHECK M1_a ....... TOTAL Result Count = 12 (12)"""
    return {name: int(count) for name, count in RULECHECK_RE.findall(text)}




def parse_magic_count(text: str) -> int | None:
    m = MAGIC_COUNT_RE.search(text)
    return int(m.group(1)) if m else None


def build_runner_script(candidates: list[str]) -> str:
    """POSIX sh script: try each license env until calibre completes.
    rc.txt values: "0" = success; "deck:<rc>" = non-license (deck/layout)
                   failure, abort immediately; "no-license" = all envs failed.
    License-failure signature is matched against the log so deck errors are
    never misclassified as license problems."""
    cand_lines = "\n".join(f'  "{c}" \\' for c in candidates).rstrip(" \\")
    return f"""#!/bin/sh
cd "$HOME/{VM_RUNDIR}" || exit 97
CANDIDATES=(
{cand_lines}
)
LIC_RE='unable to acquire|unable to (find|check out).*licen|licen[sc]e.*(denied|fail)|no license file variables'
for CAND in "${{CANDIDATES[@]}}"; do
  : > calibre.log
  export "$CAND"
  {CALIBRE_BIN} -drc -hier sg13g2_beol_gen.drc >> calibre.log 2>&1
  RC=$?
  if [ "$RC" -eq 0 ]; then
    echo "$CAND" > license_var.txt
    echo "0" > rc.txt
    exit 0
  fi
  echo "$CAND rc=$RC" >> license_attempts.log
  if ! grep -qiE "$LIC_RE" calibre.log; then
    echo "deck:$RC" > rc.txt          # real deck/layout error — stop trying
    echo "$CAND" > license_var.txt
    exit 3
  fi
done
echo "none" > license_var.txt
echo "no-license" > rc.txt
exit 98
"""


def assemble_report(*, deck: str, gds: str, rules_total: int,
                    violations: dict[str, int], log_rel: str,
                    license_var: str, magic_total: int | None,
                    extra_meta: dict) -> dict:
    nonzero = {k: v for k, v in violations.items() if v > 0}
    return {
        "tool": "calibre",
        "deck": deck,
        "gds": gds,
        "rules_total": rules_total,
        "violations": [{"rule": k, "count": v}
                       for k, v in sorted(violations.items())],
        "violations_nonzero": [{"rule": k, "count": v}
                               for k, v in sorted(nonzero.items())],
        "total_violations": sum(nonzero.values()),
        "parse_ok": True,
        "log": log_rel,
        "license_var": license_var,
        "magic_baseline_total": magic_total,
        "delta_vs_magic": {
            "magic_count": magic_total,
            "calibre_count": sum(nonzero.values()),
            "note": ("magic reports aggregate COUNT only (0 rules listed); "
                     "per-rule delta lives in docs/calibre_deck_notes.md"),
        },
        "meta": extra_meta,
    }


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def stage_gds(gds_arg: Path, outdir: Path) -> tuple[Path, str]:
    """Gunzip a working copy into <repo>/work/<wave>/ (gitignored); the
    original GDS is only ever read."""
    outdir.mkdir(parents=True, exist_ok=True)
    work = REPO_ROOT / "work" / outdir.name
    work.mkdir(parents=True, exist_ok=True)
    local_gds = work / "chip_top_logo_fill.gds"
    try:
        data = gzip.decompress(gds_arg.read_bytes())
    except OSError as e:                       # already-plain GDS or corrupt
        raise StageError("stage-gds", f"cannot read {gds_arg}: {e}") from e
    local_gds.write_bytes(data)
    primary = detect_primary(data)
    return local_gds, primary


def _gds_record_names(gds_bytes: bytes) -> tuple[set[str], set[str]]:
    """Walk GDS records; return (defined structures, referenced structures).
    Record header is 4 bytes: uint16 length (incl. header), uint16 type.
    STRNAME=0x0606 (definition), SNAME=0x1206 (SREF/AREF reference)."""
    data = memoryview(gds_bytes)
    structs: set[str] = set()
    refs: set[str] = set()
    pos, n = 0, len(data)
    while pos + 4 <= n:
        ln = int.from_bytes(data[pos:pos + 2], "big")
        rtype = int.from_bytes(data[pos + 2:pos + 4], "big")
        if ln < 4 or pos + ln > n:
            break
        if rtype in (0x0606, 0x1206):
            name = bytes(data[pos + 4:pos + ln]).decode(
                "ascii", "replace").rstrip("\x00").strip()
            (structs if rtype == 0x0606 else refs).add(name)
        pos += ln
    return structs, refs


def detect_primary(gds_bytes: bytes) -> str:
    """Top cell = defined structure that no other structure references."""
    structs, refs = _gds_record_names(gds_bytes)
    tops = sorted(structs - refs)
    if len(tops) == 1:
        return tops[0]
    if not tops and structs:                   # cyclic or flat oddity
        raise StageError("stage-gds",
                         f"no unreferenced top cell among {len(structs)} "
                         f"structures")
    raise StageError("stage-gds", f"multiple top cells: {tops[:8]}")



def vm_home() -> str:
    """Absolute home on the VM (common.vm_push single-quotes the remote path,
    so $VAR and ~ would NOT expand there — resolve it once, use absolutes)."""
    rc, out = vm_ssh("echo $HOME", timeout=30)
    home = out.splitlines()[0].strip() if out else ""
    if not home.startswith("/"):
        raise StageError("transfer:vm-push", f"cannot resolve VM HOME: {out}")
    return home


def push_vm(deck_path: Path, gds_local: Path) -> None:
    home = vm_home()
    rundir = f"{home}/{VM_RUNDIR}"
    rc, out = vm_ssh(f"mkdir -p {rundir}", timeout=30)
    if rc != 0:
        raise StageError("transfer:vm-push", out[-300:])
    gen_deck = deck_path.with_name("sg13g2_beol_gen.drc")
    for local, remote in ((gen_deck, "sg13g2_beol_gen.drc"),
                          (gds_local, "chip_top_logo_fill.gds")):
        vm_push(local, f"{rundir}/{remote}")
    rc, out = vm_ssh(
        f"ls -la {rundir}/sg13g2_beol_gen.drc "
        f"{rundir}/chip_top_logo_fill.gds", timeout=30)
    if rc != 0:
        raise StageError("transfer:vm-push", out[-300:])


def run_calibre(timeout: float) -> str:
    """Launch detached, poll, return discovered license var."""
    home = vm_home()
    rundir = f"{home}/{VM_RUNDIR}"
    runner_local = Path("/tmp/pvrun_calibre_runner.sh")
    runner_local.write_text(build_runner_script(LICENSE_CANDIDATES))
    vm_push(runner_local, f"{rundir}/runner.sh")
    rc, out = vm_ssh(
        f"cd {rundir} && rm -f rc.txt license_var.txt && "
        f"chmod +x runner.sh && nohup ./runner.sh >/dev/null 2>&1 & echo started",
        timeout=30)
    if "started" not in out:
        raise StageError("run:calibre", f"launch failed: {out[-200:]}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        rc, out = vm_ssh(
            f"cat {rundir}/rc.txt 2>/dev/null || echo __RUNNING__;"
            f" tail -3 {rundir}/license_attempts.log 2>/dev/null",
            timeout=45)
        if "__RUNNING__" not in out.splitlines()[0]:
            break
        print(f"[pvrun] still running... last license attempts: {out.strip()}",
              file=sys.stderr)
    else:
        raise StageError("run:calibre", f"timed out after {timeout}s")

    rc_line = out.splitlines()[0].strip()
    _, raw = vm_ssh(f"cat {rundir}/license_var.txt", timeout=30)
    # vm_ssh concatenates ssh stderr (host-key warnings); keep only the var
    lvar = next((l for l in raw.splitlines()
                 if re.match(r"^[A-Z_]+=", l)), "")
    if rc_line == "no-license" or not lvar or lvar.strip() == "none":
        _, tail = vm_ssh(f"tail -40 {rundir}/calibre.log", timeout=30)
        raise StageError("run:calibre-license",
                         f"all license candidates failed; log tail:\n{tail}")
    if rc_line.startswith("deck:"):
        _, tail = vm_ssh(f"tail -40 {rundir}/calibre.log", timeout=30)
        raise StageError("run:calibre",
                         f"calibre exited {rc_line[5:]} (deck/layout error, "
                         f"license={lvar.strip()}); log tail:\n{tail}")
    return lvar.strip()


def pull_results(outdir: Path) -> tuple[Path, Path]:
    home = vm_home()
    rundir = f"{home}/{VM_RUNDIR}"
    files = {}
    wanted = (("calibre.log", "calibre.log"),
              ("calibre_drc.summary", "calibre_drc.summary"),
              ("calibre_drc.db", "calibre_drc.db"),
              ("rc.txt", "rc.txt"),
              ("license_var.txt", "license_var.txt"))
    for remote, local_name in wanted:
        if remote == "calibre_drc.db":
            # results DB can be huge when a layout is dirty; keep it only
            # when reasonably small, otherwise leave it on the VM
            _, sz = vm_ssh(f"stat -c %s {rundir}/{remote} 2>/dev/null || echo 0",
                           timeout=30)
            sz = next((l for l in sz.splitlines() if l.strip().isdigit()),
                      "0")
            if int(sz) > 64 * 1024 * 1024:
                print(f"[pvrun] skipping {remote} pull ({sz.strip()} bytes)",
                      file=sys.stderr)
                continue
        try:
            data = vm_pull(f"{rundir}/{remote}")
        except StageError as e:
            raise StageError("transfer:vm-pull",
                             f"{remote}: {e.detail}") from e
        p = outdir / local_name
        p.write_bytes(data)
        files[local_name] = p
    return files["calibre.log"], files["calibre_drc.summary"]




# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def self_test() -> int:
    deck = (REPO_ROOT / "calibre/sg13g2_beol.drc").read_text()
    tmpl = (REPO_ROOT / "calibre/runsets/drc.template.runset").read_text()

    rules = rule_names_from_deck(deck)
    expected = ["M1_a", "M1_b", "M2_a", "M2_b", "V1_c", "TV1_d", "TM2_b"]
    for name in expected:
        assert name in rules, (name, rules)

    rewritten = rewrite_layout(deck, "foo.gds", "TOP")
    assert 'LAYOUT PATH       "foo.gds"' in rewritten
    assert 'LAYOUT PRIMARY    "TOP"' in rewritten
    assert rewritten != deck  # original untouched on disk

    inst = expand_runset(tmpl, deck="/x/d.drc", gds="g.gds", primary="TOP",
                         rdb="r.db", summary="s.sum")
    assert '"TOP"' in inst and "/x/d.drc" in inst
    try:
        expand_runset("keep @UNKNOWN@", deck="d")
        raise AssertionError("unexpanded token accepted")
    except StageError as e:
        assert e.stage == "expand:runset"

    sample_summary = (
        "--- RULECHECK RESULTS STATISTICS ---\n"
        "RULECHECK M1_a ....... TOTAL Result Count = 0 (0)\n"
        "RULECHECK V1_b ....... TOTAL Result Count = 12 (12)\n"
        "TOTAL CPU Time:                  3\n")
    counts = parse_summary(sample_summary)
    assert counts == {"M1_a": 0, "V1_b": 12}, counts

    assert parse_magic_count("[INFO] COUNT: 0\n") == 0
    assert parse_magic_count("[INFO] COUNT: 42\n") == 42
    assert parse_magic_count("nothing here\n") is None

    script = build_runner_script(["A=1", "B=2"])
    assert 'export "$CAND"' in script and "-drc -hier" in script
    assert '"A=1" \\' in script

    rpt = assemble_report(deck="d", gds="g", rules_total=2,
                          violations={"a": 0, "b": 3}, log_rel="l",
                          license_var="MGLS_LICENSE_FILE=x",
                          magic_total=0, extra_meta={"k": "v"})
    assert rpt["parse_ok"] is True and rpt["rules_total"] == 2
    assert rpt["total_violations"] == 3 and rpt["meta"] == {"k": "v"}

    try:
        raise StageError("run:calibre", "boom")
    except StageError as e:
        assert e.stage == "run:calibre"

    print(json.dumps({"self_test": "pass",
                      "deck_rules": len(rules),
                      "stages": ["stage-gds", "push-vm", "run-calibre",
                                 "parse-results", "compare-magic"]},
                     indent=2))
    return 0


def run(args: argparse.Namespace) -> int:
    outdir = (REPO_ROOT / args.out).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    deck_src = (REPO_ROOT / args.deck).resolve()
    gds_src = Path(args.gds).resolve()
    if not deck_src.is_file():
        raise StageError("stage-gds", f"deck missing: {deck_src}")
    if not gds_src.is_file():
        raise StageError("stage-gds", f"gds missing: {gds_src}")

    # stage-gds -------------------------------------------------------------
    t0 = time.monotonic()
    gds_local, primary = stage_gds(gds_src, outdir)
    deck_text = rewrite_layout(deck_src.read_text(),
                               gds_local.name, primary)
    gen_deck = REPO_ROOT / "work" / outdir.name / "sg13g2_beol_gen.drc"
    gen_deck.write_text(deck_text)
    rules_checked = rule_names_from_deck(deck_text)
    print(f"[pvrun] stage-gds ok: primary={primary}, "
          f"gds={gds_local.stat().st_size}B, {len(rules_checked)} checks "
          f"({time.monotonic()-t0:.1f}s)", file=sys.stderr)

    # push-vm ---------------------------------------------------------------
    t0 = time.monotonic()
    push_vm(gen_deck, gds_local)
    print(f"[pvrun] push-vm ok ({time.monotonic()-t0:.1f}s)", file=sys.stderr)

    # run-calibre -----------------------------------------------------------
    t0 = time.monotonic()
    license_var = run_calibre(args.timeout)
    print(f"[pvrun] run-calibre ok, license={license_var} "
          f"({time.monotonic()-t0:.1f}s)", file=sys.stderr)

    # parse-results ---------------------------------------------------------
    t0 = time.monotonic()
    log_p, sum_p = pull_results(outdir)
    counts = parse_summary(sum_p.read_text(errors="replace"))
    if not counts:
        tail = "\n".join(log_p.read_text(errors="replace").splitlines()[-25:])
        raise StageError("parse:calibre", f"no RULECHECK lines parsed;\n{tail}")
    print(f"[pvrun] parse-results ok: {len(counts)} rule families "
          f"({time.monotonic()-t0:.1f}s)", file=sys.stderr)

    # compare-magic ---------------------------------------------------------
    magic_rpt = Path("/home/rfsoc/exp/rfic/LinHT-rfic/verification/drc/"
                     "chip_top_logo_fill.magic.drc.rpt")
    magic_total = None
    if magic_rpt.is_file():
        try:
            magic_total = parse_magic_count(magic_rpt.read_text())
        except StageError:
            pass

    report = assemble_report(
        deck=str(deck_src.relative_to(REPO_ROOT)),
        gds=str(gds_src), rules_total=len(rules_checked),
        violations=counts, log_rel=str(outdir / "calibre.log"),
        license_var=license_var, magic_total=magic_total,
        extra_meta={
            "harness": "pvrun.py",
            "rundir_on_vm": f"~/{VM_RUNDIR}",
            "calibre_cmd": f"{CALIBRE_BIN} -drc -hier sg13g2_beol_gen.drc",
            "rules_checked": rules_checked,
            "magic_baseline_report": str(magic_rpt) if magic_rpt.is_file()
                                     else "missing",
            "subset_note": "wave-1 BEOL subset; see docs/calibre_deck_notes.md",
        })
    rpt_path = outdir / "report.json"
    rpt_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(rpt_path),
                      "total_violations": report["total_violations"],
                      "nonzero_rules": len(report["violations_nonzero"]),
                      "license_var": license_var}, indent=2))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--deck", default="calibre/sg13g2_beol.drc")
    ap.add_argument("--gds", default=None)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.gds:
        ap.error("--gds is required unless --self-test")
    try:
        return run(args)
    except StageError as e:
        print(json.dumps({"stage_error": {"stage": e.stage,
                                          "detail": e.detail}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

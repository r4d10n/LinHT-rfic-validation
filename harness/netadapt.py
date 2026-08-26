#!/usr/bin/env python3
"""G002 netlist adapter: repo ngspice decks -> hpeesofsim (ADS) and Spectre.

Dialect facts this encodes (all empirically established):
- ADS hpeesofsim rejects `*` comment lines -> emitted as `;`.
- ADS subckts are `define NAME (pins) parameters ... end NAME`; ngspice
  `.subckt/.ends` translated.
- ngspice primitive lines `M<name> d g s b MODEL w=..` become
  `MODEL:M<name> d g s b w=..` for ADS; Spectre keeps model-as-5th-token only
  for native models, so MOS instances reference the wrapper subckt via X-style
  calls in BOTH targets (the IHP wrappers sg13_lv_nmos/pmos are subckts, not
  .model cards).
- The IHP MOS "models" are OpenVAF psp103va cards (osdi in ngspice). For
  Spectre the same card text is emitted as `model <name> <va-module> ...`
  against an `ahdl_include` of psp103.va; expression strings `'e'` become
  `( e )`.
- Corner selection (.lib cornerMOSlv.lib mos_tt etc.) is pre-expanded here into
  plain `parameters` globals from the vendored libs (reference/pdk_models/).
- `.control` blocks are dropped: metrics are computed host-side by simrun.py
  from raw waveforms, identically for all three simulators.

--self-test exercises pure transforms without VM/container access.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ADS_ENV_TEXT, REPO_ROOT, StageError

PDK_REF = REPO_ROOT / "reference/pdk_models"
LINHT_REPO = REPO_ROOT.parent / "LinHT-rfic"

# corner section name per corner, per library (from IHP corner*.lib contents)
CORNER_SECTIONS = {
    "tt": {"mos": "mos_tt", "res": "res_typ", "cap": "cap_typ"},
    "ss": {"mos": "mos_ss", "res": "res_wcs", "cap": "cap_wcs"},
    "ff": {"mos": "mos_ff", "res": "res_bcs", "cap": "cap_bcs"},
    "sf": {"mos": "mos_sf", "res": "res_typ", "cap": "cap_typ"},
    "fs": {"mos": "mos_fs", "res": "res_typ", "cap": "cap_typ"},
}
ADS_CORNER_NET = "~/linht/ads/pdk/sg13g2_{corner}.net"  # pre-staged on VM


# ---------------------------------------------------------------- TB parsing
def read_deck(path: Path) -> list[str]:
    text = path.read_text()
    # join continuation lines (+ ...) so translators see logical lines
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.rstrip()
        if not out and s.strip().startswith("*"):
            out.append(s)
            continue
        if s.lstrip().startswith("+") and out:
            out[-1] += " " + s.lstrip()[1:].strip()
        else:
            out.append(s)
    return out


def parse_tb(tb_path: Path) -> dict:
    """Split a TB deck into header comments, includes, params, temp, body."""
    lines = read_deck(tb_path)
    doc = {"includes": [], "params": [], "temp": 27, "body": [],
           "control": False, "analyses": []}
    in_ctrl = False
    for ln in lines:
        st = ln.strip()
        low = st.lower()
        if low.startswith(".control"):
            in_ctrl = True
            continue
        if low.startswith(".endc"):
            in_ctrl = False
            continue
        if in_ctrl:
            doc["control"] = True
            continue
        if not st or st.startswith("*"):
            continue
        if low.startswith(".lib ") or low.startswith(".include "):
            doc["includes"].append(st)
        elif low.startswith(".param"):
            body = st[6:].strip().strip("{}")
            doc["params"].append(body)
        elif low.startswith(".temp"):
            doc["temp"] = float(st.split()[1])
        elif low.startswith(".tran"):
            t = st.split()[1:]
            doc["analyses"].append({"type": "tran", "tstep": t[0],
                                    "tstop": t[1] if len(t) > 1 else t[0]})
        elif low.startswith(".op"):
            doc["analyses"].append({"type": "op"})
        elif low.startswith((".end", ".options")):
            continue
        else:
            doc["body"].append(ln)
    return doc


def resolve_includes(tb_path: Path, includes: list[str]) -> list[Path]:
    """Expand $PDK_ROOT model includes vs repo-relative circuit includes.

    Candidates tried in order: TB-relative path, LinHT-rfic-root-relative
    (after stripping leading ../), then any macros/*/netlist/schematic match
    by filename. Last one makes fixtures and relocated TBs work."""
    files = []
    tb_dir = tb_path.parent
    for inc in includes:
        target = inc.split(None, 1)[1].strip()
        if "$PDK_ROOT" in target or target.endswith(".lib"):
            continue  # model libs handled by bundle generator
        cands = [(tb_dir / target).resolve(),
                 (LINHT_REPO / target.replace("../", "")).resolve()]
        cands += list((LINHT_REPO / "macros").glob(f"*/netlist/schematic/{Path(target).name}"))
        hit = next((c for c in cands if c.exists()), None)
        if hit is None:
            raise StageError("adapt:include-missing", f"{target} from {tb_path}")
        files.append(hit)
    return files


def stage_circuit_files(files: list[Path], staging: Path) -> list[str]:
    """Copy circuit includes flatly into staging; returns staged filenames."""
    staging.mkdir(parents=True, exist_ok=True)
    names = []
    for f in files:
        dest = staging / f.name
        dest.write_text(f.read_text())
        names.append(f.name)
    return names


def rename_staged_for_spectre(staging: Path, names: list[str]) -> list[str]:
    """Spectre picks the reader by extension: .spice forces SPICE mode and
    .spc is its COMPILED-TABLE format (CMI-3078 interp=bbspice). Use .scs."""
    out = []
    for n in names:
        src, dst = staging / n, staging / (n + ".scs")
        src.rename(dst)
        out.append(dst.name)
    return out


# ------------------------------------------------------- corner expansion
def lib_section(path: Path, section: str) -> list[tuple[str, str]]:
    text = path.read_text(errors="replace")
    m = re.search(r"\.LIB\s+" + re.escape(section) + r"\s*\n(.*?)\.ENDL",
                  text, re.S | re.I | re.M)
    if not m:
        raise StageError("adapt:lib-section", f"{section} not in {path.name}")
    pairs = re.findall(r"\.param\s+([A-Za-z_]\w*)\s*=\s*([-\w.+]+)", m.group(1), re.I)
    return [(n.lower(), v) for n, v in pairs]


def corner_globals(corner: str) -> list[tuple[str, str]]:
    sec = CORNER_SECTIONS[corner]
    g = []
    g += lib_section(PDK_REF / "cornerMOSlv.lib", sec["mos"])
    g += lib_section(PDK_REF / "cornerRES.lib", sec["res"])
    g += lib_section(PDK_REF / "cornerCAP.lib", sec["cap"])
    return g


# ------------------------------------------------------------ emitters
def emit_params_ngstyle(params: list[str]) -> str:
    """ngspice `.param a=1 b={expr}` bodies joined."""
    return " ".join(p.strip() for p in params if p)


def debrace(line: str) -> str:
    """Strip ngspice {expr} braces; k={v} stays k=v."""
    return line.replace("{", "").replace("}", "")


def _split6(argstr: str) -> list[str]:
    """ngspice sin(...) args: VO VA FREQ TD THETA PHASE (pad missing 0s)."""
    a = [t for t in argstr.split() if t]
    while len(a) < 6:
        a.append("0")
    return a[:6]


def _join_cont_lines(text: str) -> str:
    """Merge SPICE `+` continuation lines into their previous line."""
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.rstrip()
        if s.lstrip().startswith("+") and out:
            out[-1] += " " + s.lstrip()[1:].strip()
        else:
            out.append(s)
    return "\n".join(out)


def _ads_safe(line: str) -> str:
    """ADS runs a C preprocessor over netlists BEFORE parsing; a `/*`
    anywhere (even inside `;` comment prose) opens an unterminated comment
    that silently swallows the rest of the file."""
    return debrace(line).replace("/*", "/\\*")
def translate_staged(text: str, target: str) -> str:
    """Rewrite a staged repo netlist for the target dialect.

    Repo macro nets are uniform: .subckt NAME pins k=v / .ends NAME, all
    devices as X-calls to wrapper subckts, {braced} instance params."""
    text = _join_cont_lines(text)
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith(".subckt"):
            toks = s.split()
            # split positional pins from params (first token containing '=')
            pins, params = [], []
            for t in toks[2:]:
                (params if "=" in t else pins).append(t)
            if target == "ads":
                head = f"define {toks[1]} ({' '.join(pins)})"
                out.append(debrace(head))
                if params:
                    out.append("parameters " + debrace(" ".join(params)))
            else:
                # spectre form (verified on samjna): nodes bare, then a body
                # `parameters` line; no parens, no keyword on the header.
                out.append(f"subckt {toks[1]} {' '.join(pins)}")
                if params:
                    out.append("parameters " + debrace(" ".join(params)))
            continue
        if low.startswith(".ends"):
            name = s.split()[1] if len(s.split()) > 1 else ""
            out.append(f"end {name}".strip() if target == "ads" else f"ends {name}".strip())
            continue
        if target == "spectre" and s.startswith("*"):
            out.append("//" + s[1:])
            continue
        if target == "ads":
            if s.startswith("*"):
                out.append("; " + s[1:])
                continue
            # V sources -> native V_Source: form (proven on samjna):
            #   dc VAL               -> Vdc=VAL
            #   pulse(V1 V2 TD TR TF PW PER)
            #                        -> Vdc=V1 V_Tran=pulse(time,...)
            #   dc VO sin(VO VA F TD TH PH)  (ngspice degrees/rad formula)
            m = re.match(r"([Vv]\w+)\s+(\S+)\s+(\S+)\s+(.*)$", s)
            if m and not low.startswith("."):
                name, n1, n2, rest = m.groups()
                mp = re.match(r"dc\s+(\S+)\s+sin\((.*)\)\s*$", rest)
                mpulse = re.match(r"pulse\((.*)\)\s*$", rest)
                mpdc = re.match(r"dc\s+(\S+)\s*$", rest)
                if mp:
                    vo, va, f, td, th, ph = _split6(debrace(mp.group(2)))
                    phase = "" if ph.strip() in ("0", "") else \
                        f"+({ph})*pi/180"
                    delay = "" if td.strip() in ("0", "") else \
                        f"(time-({td}))"
                    texpr = f"{va}*sin(2*pi*{f}*{delay or 'time'}{phase})"
                    if td.strip() not in ("0", "") and th.strip() != "0":
                        texpr = f"({texpr})*exp(-({td})*({th}))"  # approx: damping after delay
                    out.append(f"V_Source:{name} {n1} {n2} Vdc={vo} "
                               f"V_Tran={texpr}")
                    continue
                if mpulse:
                    a = debrace(mpulse.group(1)).split()
                    out.append(f"V_Source:{name} {n1} {n2} Vdc={a[0]} "
                               "V_Tran=pulse(time," + ",".join(a) + ")")
                    continue
                if mpdc:
                    out.append(f"V_Source:{name} {n1} {n2} Vdc={debrace(mpdc.group(1))}")
                    continue
            # plain R/C primitives -> R:/C: named forms
            mr = re.match(r"[Rr](\w+)\s+(\S+)\s+(\S+)\s+(\S+)$", s)
            mc = re.match(r"[Cc](\w+)\s+(\S+)\s+(\S+)\s+(\S+)$", s)
            if mr and "=" not in mr.group(4):
                out.append(f"R:{mr.group(1)} {mr.group(2)} {mr.group(3)} "
                           f"R={debrace(mr.group(4))}")
                continue
            if mc and "=" not in mc.group(4):
                out.append(f"C:{mc.group(1)} {mc.group(2)} {mc.group(3)} "
                           f"C={debrace(mc.group(4))}")
                continue
            # X-calls -> native SubcktName:InstName form
            m = re.match(r"([Xx])(\S+)\s+(.*)$", s)
            if m and not low.startswith("."):
                toks = m.group(3).split()
                seen_param = False
                body, params = [], []
                for t in toks:
                    if "=" in t or seen_param:
                        seen_param = True
                        params.append(t)
                    else:
                        body.append(t)
                nodes, subname = body[:-1], body[-1]
                out.append(debrace(f"{subname}:{m.group(2)} {' '.join(nodes)} {' '.join(params)}".rstrip()))
                continue
            out.append(debrace(ln))
            continue
        # ---- spectre branch element translation ----
        ms = re.match(r"([Vv]\w+)\s+(\S+)\s+(\S+)\s+(.*)$", s)
        if ms:
            name, n1, n2, rest = ms.groups()
            mp = re.match(r"dc\s+(\S+)\s+sin\((.*)\)\s*$", rest)
            mpulse = re.match(r"pulse\((.*)\)\s*$", rest)
            mpdc = re.match(r"dc\s+(\S+)\s*$", rest)
            if mp:
                vo, va, f_, td, th, ph = _split6(debrace(mp.group(2)))
                sp = "" if ph.strip() in ("0", "") else \
                    f" sinphase=({ph})*pi/180"
                out.append(f"{name} {n1} {n2} vsource type=sine "
                           f"sinedc={vo} ampl={va} freq={f_}{sp}")
                continue
            if mpulse:
                a = debrace(mpulse.group(1)).split()
                out.append(f"{name} {n1} {n2} vsource type=pulse "
                           f"v1={a[0]} v2={a[1]} delay={a[2]} rise={a[3]} "
                           f"fall={a[4]} width={a[5]} period={a[6]}")
                continue
            if mpdc:
                out.append(f"{name} {n1} {n2} vsource type=dc dc={debrace(mpdc.group(1))}")
                continue
        mr = re.match(r"[Rr](\w+)\s+(\S+)\s+(\S+)\s+(\S+)$", s)
        mc = re.match(r"[Cc](\w+)\s+(\S+)\s+(\S+)\s+(\S+)$", s)
        mx = re.match(r"([Xx])(\S+)\s+(.*)$", s)
        if mr and "=" not in mr.group(4):
            out.append(debrace(f"{mr.group(1)} {mr.group(2)} {mr.group(3)} resistor r={mr.group(4)}"))
            continue
        if mc and "=" not in mc.group(4):
            out.append(debrace(f"{mc.group(1)} {mc.group(2)} {mc.group(3)} capacitor c={mc.group(4)}"))
            continue
        if mx:
            body = mx.group(3).split()
            seen = False; nodes=[]; params=[]
            for t in body:
                if "=" in t or seen: seen=True; params.append(t)
                else: nodes.append(t)
            subname = nodes[-1]
            out.append(debrace(f"{mx.group(2)} {' '.join(nodes[:-1])} {subname} {' '.join(params)}".rstrip()))
            continue
        out.append(debrace(ln))
    if target == "ads":
        return "\n".join(_ads_safe(l) for l in out) + "\n"
    return "\n".join(out) + "\n"


def gen_ads(doc: dict, corner: str, extra_defs: list[str]) -> list[str]:
    """hpeesofsim deck. Reuses the VM-staged ihp2ads corner bundles."""
    o = [f"; generated by netadapt.py --target ads (corner {corner})"]
    o.append(f'#include "{ADS_CORNER_NET.format(corner=corner)}"')
    o.append(f'Options TopDesignName="linht_xcheck" Temp={doc["temp"]}')
    # ADS top-level parameters: one name=value per line
    for p in doc["params"]:
        for kv in p.split():
            if "=" in kv:
                o.append(kv)
    o += extra_defs
    body_lines = translate_staged("\n".join(doc["body"]), "ads").splitlines()
    for ln in body_lines:
        s = ln.strip()
        if s.startswith("*"):
            o.append(";" + s[1:])
        else:
            o.append(s)
    for a in doc["analyses"]:
        if a["type"] == "tran":
            o.append(f'Tran:tran1 StopTime={debrace(a["tstop"])} '
                     f'MaxTimeStep={debrace(a["tstep"])}')
        elif a["type"] == "op":
            # OP via a degenerate tran: identical metric extraction across sims
            o.append("Tran:tran1 StopTime=1n MaxTimeStep=1n")
    return [_ads_safe(l) for l in o]


_SPECTRE_WRAPPERS = """
// IHP device wrappers, Spectre dialect (grammar verified on samjna:
// subckt NAME nodes... / body parameters line / ends NAME).
subckt sg13_lv_nmos d g s b
parameters w=0.35e-6 l=0.34e-6 ng=1 m=1 trise=0 z1=0.34e-6 z2=0.38e-6 wmin=0.15e-6
  parameters wf=max(w/ng,wmin)
  parameters odd=(floor(floor(ng/2+0.501)*2+0.001) != ng)
  parameters as_o=wf*(z1+((ng-1)/2)*z2)
  parameters ps_o=2*(wf*((ng-1)/2+1)+z1+(ng-1)/2*z2)
  parameters as_e=wf*(2*z1+max(0,(ng-2)/2)*z2)
  parameters ad_e=wf*z2/2*ng
  parameters ps_e=2*(wf*(2+max(ng-2,0)/2)+2*z1+max(ng-2,0)/2*z2)
  parameters pd_e=(wf+z2)*ng
  N1 (d g s b) sg13g2_lv_nmos_psp w=w l=l nf=ng mult=m as=(odd ? as_o : as_e) ad=(odd ? as_o : ad_e) ps=(odd ? ps_o : ps_e) pd=(odd ? ps_o : pd_e) dta=trise ngcon=2 delvto=0 factuo=1
ends sg13_lv_nmos

subckt sg13_lv_pmos d g s b
parameters w=0.35e-6 l=0.34e-6 ng=1 m=1 trise=0 z1=0.34e-6 z2=0.38e-6 wmin=0.15e-6
  parameters wf=max(w/ng,wmin)
  parameters odd=(floor(floor(ng/2+0.501)*2+0.001) != ng)
  parameters as_o=wf*(z1+((ng-1)/2)*z2)
  parameters ps_o=2*(wf*((ng-1)/2+1)+z1+(ng-1)/2*z2)
  parameters as_e=wf*(2*z1+max(0,(ng-2)/2)*z2)
  parameters ad_e=wf*z2/2*ng
  parameters ps_e=2*(wf*(2+max(ng-2,0)/2)+2*z1+max(ng-2,0)/2*z2)
  parameters pd_e=(wf+z2)*ng
  N1 (d g s b) sg13g2_lv_pmos_psp w=w l=l nf=ng mult=m as=(odd ? as_o : as_e) ad=(odd ? as_o : ad_e) ps=(odd ? ps_o : ps_e) pd=(odd ? ps_o : pd_e) dta=trise ngcon=2 delvto=0 factuo=1
ends sg13_lv_pmos

subckt rppd p n bn
parameters w=0.5e-6 l=0.5e-6 b=0 ps=0.18e-6
  parameters weff=w+0.006e-6
  parameters rtot=(rsh_rppd*((b+1)*l+ps*b)/weff + 2*52*0.5e-6/weff)
  R1 (p n) resistor r=rtot tc1=0.000170 tc2=6.0e-7
ends rppd

subckt rhigh p n bn
parameters w=0.5e-6 l=0.96e-6 b=0 ps=0.18e-6
  parameters weff=w-0.04e-6
  parameters rtot=(rsh_rhigh*((b+1)*l+ps*b)/weff + 2*55*0.5e-6/weff)
  R1 (p n) resistor r=rtot tc1=-0.002300 tc2=3.0e-6
ends rhigh

subckt rsil p n bn
parameters w=0.5e-6 l=0.5e-6 b=0 ps=0.18e-6
  parameters weff=w+0.01e-6
  parameters rtot=(rsh_rsil*((b+1)*l+ps*b)/weff + 2*1.5*0.5e-6/weff)
  R1 (p n) resistor r=rtot tc1=0.003100 tc2=2.0e-7
ends rsil

subckt cap_cmim p n
parameters w=7e-6 l=7e-6 mm_ok=0
  parameters ctot=(cap_carea*w*l*1e12 + 40e-18*2*(w+l))
  Rs (p x) resistor r=0.055
  C1 (x n) capacitor c=ctot
ends cap_cmim
"""


def _dequote(expr: str) -> str:
    """ngspice 'expr' quoting -> parenthesised spectre expression."""
    e = expr.strip()
    if e.startswith("'") and e.endswith("'"):
        return "( " + e[1:-1] + " )"
    return e


def _translate_model_card(line: str, va_module: str) -> str:
    """.model NAME psp103va k=v k='e' ... -> model NAME <module> k=v k=( e ) ...
    type=+1/-1 kept verbatim (VA integer param)."""
    m = re.match(r"\.?model\s+(\S+)\s+(\S+)\s*(.*)$", line, re.I)
    if not m:
        return line
    name, oldmod, rest = m.group(1), m.group(2), m.group(3)
    toks = []
    for kv in re.finditer(r"([A-Za-z_]\w*)\s*=\s*('[^']*'|\"[^\"]*\"|[^\s]+)", rest):
        toks.append(f"{kv.group(1)}={_dequote(kv.group(2))}")
    return f"model {name} {va_module} " + " ".join(toks)


def gen_spectre_bundle(corner: str, va_module_map: dict[str, str]) -> str:
    """Generate models_sg13g2_<corner>.spectre from vendored PDK libs."""
    # NOTE: no ahdl_include — Spectre 25.1 has a native `psp103` primitive;
    # including the VA shadowed it and broke initial setup (CMI-3078).
    o = ["simulator lang=spectre", ""]
    g = corner_globals(corner)
    g.append(("pre_layout", "1"))
    o.append("parameters " + " ".join(f"{k}={v}" for k, v in g))
    o.append("")
    o.append(_SPECTRE_WRAPPERS)
    o.append("// ---- PSP103 VA model cards (from sg13g2_moslv_parm.lib) ----")
    parm = (PDK_REF / "sg13g2_moslv_parm.lib").read_text(errors="replace")
    def _strip_geom(card: str) -> str:
        """Remove geometry-dependent model params (balanced-paren aware;
        spectre evaluates model-scope exprs without w/l/ng)."""
        for key in ("iginvlw", "cfrw"):
            while True:
                i = card.find(key + "=")
                if i < 0:
                    break
                j = card.find("(", i)
                if j < 0:
                    break
                depth, k = 0, j
                while k < len(card):
                    if card[k] == "(":
                        depth += 1
                    elif card[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                end = k + 1
                while end < len(card) and card[end] == " ":
                    end += 1
                card = card[:i].rstrip() + " " + card[end:]
        return card

    last_was_card = False
    pending = None
    for raw in parm.splitlines():
        s = raw.strip()
        if s.lower().startswith(".model"):
            modname = s.split()[1].lower()
            va_mod = va_module_map.get(modname)
            if va_mod is None:
                last_was_card = False
                continue  # hv/rf variants unused by current waves
            pending = _translate_model_card(s, va_mod)
            last_was_card = True
        elif s.startswith("+") and last_was_card:
            body = " ".join(_translate_model_card("model X Y " + s[1:], "X").split()[3:])
            pending += " " + body
        elif last_was_card and pending is not None:
            o.append(_strip_geom(pending))
            pending = None
            last_was_card = False
    if pending is not None:
        o.append(_strip_geom(pending))
    return "\n".join(o) + "\n"


VA_MODULE_MAP = {
    "sg13g2_lv_nmos_psp": "psp103",
    "sg13g2_lv_pmos_psp": "psp103",
}


def gen_spectre(doc: dict, corner: str, staged_names: list[str]) -> list[str]:
    o = [f"LinHT xcheck {corner} (netadapt spectre)",
         "simulator lang=spectre",
         f'include "models_sg13g2_{corner}.spectre"',
         f"simopts options temp={doc['temp']:g}"]
    for nm in staged_names:
        o.append(f'include "{nm}"')
    if doc["params"]:
        o.append("parameters " + debrace(emit_params_ngstyle(doc["params"])))
    for ln in translate_staged("\n".join(doc["body"]), "spectre").splitlines():
        s2 = ln.strip()
        if s2.startswith("*"):
            continue  # spectre comments are //; drop star-comments entirely
        o.append(s2)
    for a in doc["analyses"]:
        if a["type"] == "tran":
            o.append(f'tran1 tran stop={debrace(a["tstop"])} '
                     f'maxstep={debrace(a["tstep"])}')
        elif a["type"] == "op":
            o.append("tran1 tran stop=1n maxstep=1n")
    return o


# ------------------------------------------------------------ driver
def adapt(tb: Path, target: str, corner: str, staging: Path,
          param_overrides: dict[str, str] | None = None) -> Path:
    if target not in ("ads", "spectre"):
        raise StageError("adapt:target", f"unknown target {target}")
    doc = parse_tb(tb)
    if param_overrides:
        # merge: overrides REPLACE same-name keys from the deck's .param lines
        merged: dict[str, str] = {}
        for p in doc["params"]:
            for kv in p.split():
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    merged[k] = v
        merged.update(param_overrides)
        doc["params"] = [f"{k}={v}" for k, v in merged.items()]
    incs = resolve_includes(tb, doc["includes"])
    staged = stage_circuit_files(incs, staging)
    if target == "spectre":
        staged = rename_staged_for_spectre(staging, staged)

    # pull psp103.va next to the bundle (Spectre compiles it at run time)
    if target == "spectre":
        va_src = staging / "psp103.va"
        if not va_src.exists():
            import subprocess
            r = subprocess.run(
                ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", "-p", "2222",
                 "rakesh@127.0.0.1", "cat ~/linht/ads/pdk/va/psp103.va"],
                capture_output=True,
                env={"SSHPASS": __import__("common")._vm_pass(),
                     "PATH": "/usr/bin:/bin:/usr/local/bin"})
            if r.returncode != 0:
                raise StageError("transfer:psp103.va", r.stderr.decode()[-200:])
            va_src.write_bytes(r.stdout)
        bundle = staging / f"models_sg13g2_{corner}.spectre"
        bundle.write_text(gen_spectre_bundle(corner, VA_MODULE_MAP))

    # translate staged repo netlists into the target dialect, in place
    for n in staged:
        p = staging / n
        body = translate_staged(p.read_text(), target)
        if target == "spectre":
            # each include resets to its per-file default language, and an
            # included file has NO title line (line 1 would parse as an
            # instance — a literal 'SPC staged netlist' once did!)
            body = ("simulator lang=spectre\n" + body)
        p.write_text(body)

    if target == "ads":
        # canonical VM-side ADS environment; sourced by simrun's launcher
        adsenv = staging / "adsenv.sh"
        if not adsenv.exists():
            adsenv.write_text(ADS_ENV_TEXT + "\n")

    if target == "ads":
        lines = gen_ads(doc, corner, [f'#include "{n}"' for n in staged])
    else:
        lines = gen_spectre(doc, corner, staged)
    out = staging / f"{'ads' if target == 'ads' else 'spc'}_{tb.stem}.net"
    out.write_text("\n".join(lines) + "\n")
    return out


def self_test() -> int:
    """Pure-transform checks with fixtures; no VM, no container."""
    tmp = Path("/tmp/netadapt_selftest")
    tmp.mkdir(exist_ok=True)

    # continuation-line joining
    d = tmp / "join.deck"
    d.write_text("* c\nVx a 0 pulse(0 1\n+ 10p 10p 1n 2n)\n")
    assert "pulse(0 1 10p 10p 1n 2n)" in "".join(read_deck(d)), read_deck(d)

    # control block dropped, params kept
    d2 = tmp / "tb.spice"
    d2.write_text("""* tb
.param fin=4.4e9 vpp=0.4
.temp 27
.include ../../netlist/schematic/lodiv_top.spice
Vdd vdd 0 dc 1.5
.tran 2p 33n
.control
save v(lo_i)
wrdata out.csv v(lo_i)
.endc
.end
""")
    doc = parse_tb(d2)
    assert doc["control"] is True and len(doc["body"]) == 1, doc
    assert doc["analyses"][0]["tstop"] == "33n"
    # include resolution against real repo layout
    incs = resolve_includes(d2, doc["includes"])
    assert incs and incs[0].name == "lodiv_top.spice", incs

    # corner expansion: tt must yield the known ctl global
    g = dict(corner_globals("tt"))
    assert g.get("sg13g2_lv_nmos_ctl") == "1.2080", g.get("sg13g2_lv_nmos_ctl")
    assert g.get("rsh_rppd") == "260.0"

    # ADS emission: * comments translated, Options line present, no .control
    ads = gen_ads(doc, "tt", [])
    txt = "\n".join(ads)
    assert "Options TopDesignName=" in txt and "Temp=27" in txt
    assert "#include" in txt and ".control" not in txt
    assert "Tran:tran1 StopTime=33n MaxTimeStep=2p" in txt
    assert not any(l.startswith("*") for l in ads)

    # spectre bundle: cards translated, module swapped, quotes dequoted
    bun = gen_spectre_bundle("tt", VA_MODULE_MAP)
    assert "simulator lang=spectre" in bun and "subckt sg13_lv_nmos" in bun
    assert "ahdl_include" not in bun
    assert "model sg13g2_lv_nmos_psp psp103" in bun
    assert ".model" not in bun
    assert "'-" not in bun.split("model sg13g2_lv_nmos_psp")[1][:400]

    print(json.dumps({"self_test": "pass"}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--tb", type=Path)
    ap.add_argument("--target", choices=["ads", "spectre"])
    ap.add_argument("--corner", default="tt")
    ap.add_argument("--staging", type=Path)
    ap.add_argument("--param", action="append", default=[],
                    help="k=v override, repeatable")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    overrides = dict(kv.split("=", 1) for kv in args.param)
    out = adapt(args.tb, args.target, args.corner, args.staging, overrides)
    print(json.dumps({"ok": True, "netlist": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

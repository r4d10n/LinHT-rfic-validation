# Calibre BEOL deck — notes from wave 1

First-ever Siemens-Calibre physical-verification path for the IHP SG13G2 open
PDK. Companion evidence: `evidence/wave1_drc/` (report.json, calibre.log,
calibre_drc.summary). Deck: `calibre/sg13g2_beol.drc`; runner:
`harness/pvrun.py`; runset template: `calibre/runsets/drc.template.runset`.

## 1. License discovery (was unverified by design)

| Variable | Value | Result |
|---|---|---|
| **MGLS_LICENSE_FILE** | **1717@10.180.60.101** | **WORKS — full DRC run completed (rc=0)** |
| LM_LICENSE_FILE | 1717@10.180.60.101 | never needed (first candidate succeeded); expected to work as fallback |
| (none) | — | "No license file variables are set / Unable to set the license server path"; rule-file compilation still runs pre-license, so a syntax-only deck test can pass with no license |

Calibre tree: `/opt/siemens/calibre`, version `v2026.3_27.19` (VM samjna).
The siemens-dli server (10.180.60.101:1717) is already probed by
`harness/env_probe.py` (`license.siemens-dli`).

## 2. Rule sources of truth

Transcribed by hand from the KLayout Ruby decks inside container `linht_iic`
(`$PDK_ROOT/ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol/`), values
cross-checked against `rule_decks/sg13g2_tech_default.json` → `drc_rules`.
Layer numbers from `rule_decks/layers_def.drc` (identical to `sg13g2.lyt`).

## 3. Rule-mapping table (KLayout id → SVRF check)

SVRF check names are the KLayout ids with `.` → `_`. Fixed-size via rules are
split in two because SVRF has no bbox-equality primitive:

* `_a_w`: `INT < v` — any dimension below nominal v fails;
* `_a_A`: `AREA > v²` — anything bigger than the nominal square fails.

A non-square rectangle passes both only when both dimensions equal v, which
reproduces KLayout's `without_bbox_min(v)/without_bbox_max(v)` exactly for
rectilinear vias.

| KLayout id | Source deck | Check | Value (um) | SVRF |
|---|---|---|---|---|
| M1.a | 5_16_metal1.drc | M1 min width | 0.16 | `M1_a { INT M1 < 0.16 }` |
| M1.b | 5_16_metal1.drc | M1 drw+fill space/notch | 0.18 | `M1_b { EXT M1DF < 0.18 }` |
| M2.a/M3.a/M4.a/M5.a | 5_17_metaln.drc | Mn min width | 0.20 | `Mx_a` |
| M2.b..M5.b | 5_17_metaln.drc | Mn drw+fill space/notch | 0.21 | `Mx_b` |
| V1.a | 5_19_via1.drc | Via1 size == 0.19 | 0.19 | `V1_a_w`, `V1_a_A` |
| V1.b | 5_19_via1.drc | Via1 space | 0.22 | `V1_b` |
| V1.c | 5_19_via1.drc | M1 enclosure of Via1 | 0.01 | `V1_c { ENC V1NS M1 < 0.01 }` |
| V2.a/V2.b/V2.c | 5_20_vian.drc | Via2 size/space/enc(M2) | 0.19/0.22/0.005 | `V2_*` |
| V3.a/V3.b/V3.c | 5_20_vian.drc | Via3 size/space/enc(M3) | 0.19/0.22/0.005 | `V3_*` |
| V4.a/V4.b/V4.c | 5_20_vian.drc | Via4 size/space/enc(M4) | 0.19/0.22/0.005 | `V4_*` |
| TV1.a | 5_21_topvia1.drc | TopVia1 size == 0.42 | 0.42 | `TV1_a_w`, `TV1_a_A` |
| TV1.b | 5_21_topvia1.drc | TopVia1 space | 0.42 | `TV1_b` |
| TV1.c | 5_21_topvia1.drc | M5 enclosure of TopVia1 | 0.10 | `TV1_c` |
| TV1.d | 5_21_topvia1.drc | TM1 enclosure of TopVia1 | 0.42 | `TV1_d` |
| TV2.a | 5_24_topvia2.drc | TopVia2 size == 0.90 | 0.90 | `TV2_a_w`, `TV2_a_A` |
| TV2.b | 5_24_topvia2.drc | TopVia2 space | 1.06 | `TV2_b` |
| TV2.c | 5_24_topvia2.drc | TM1 enclosure of TopVia2 | 0.50 | `TV2_c` |
| TV2.d | 5_24_topvia2.drc | TM2 enclosure of TopVia2 | 0.50 | `TV2_d` |
| TM1.a | 5_22_topmetal1.drc | TM1 min width | 1.64 | `TM1_a` |
| TM1.b | 5_22_topmetal1.drc | TM1 drw+fill space/notch | 1.64 | `TM1_b` |
| TM2.a | 5_25_topmetal2.drc | TM2 min width | 2.00 | `TM2_a` |
| TM2.b | 5_25_topmetal2.drc | TM2 drw+fill space/notch | 2.00 | `TM2_b` |

Edge-seal exclusion: every via layer is masked with
`<VIA>NS = <VIA> NOT EDGESEAL` (39/0), mirroring `via_lay.not(edgeseal_drw)`.

### Deliberately out of scope (wave-1 subset policy)

* Conditional/wide-metal spacing: M1.e/f/g/i, Mn.e/f/g/i, TM2.bR (recommended).
* All FEOL, density (density.drc), antenna, filler-table rules
  (5_18/5_23/5_26), pad/pillar/bump (6_9), MIM (6_11), seal ring, slits.
* V1.a's `npn13g2l` exclusion marker: that device layer does not occur in
  SG13G2 GDS output of this flow.
* Density checks are excluded by design ("density-independent" acceptance);
  note magic does not check density either, so this blind spot is shared.

## 4. SVRF dialect findings (verified empirically on VM, Calibre v2026.3_27.19)

Each item below was confirmed with a minimal probe deck + synthetic layout
(`~/pv_probe/t*.drc` on the VM; probe GDS built with klayout pya in
container linht_iic):

1. **No `WIDTH` / `SPACE` keywords** (unlike KLayout): minimum width is
   `INT layer < v`; spacing/notch is `EXT layer < v`.
2. **Brace-named rule checks**: `Name { OP ... }`; the name appears verbatim
   as `RULECHECK Name` in the summary report. Assigning a measure op
   (`X = WIDTH ...`) is a SYN1 error in this build.
3. **`ENC A B < d`: A is the enclosed layer, B the enclosing layer.**
   Verified with a synthetic via whose metal enclosure is 0.005 um on one
   side: only `ENC VIA METAL < 0.01` flagged it. So "Metal1 enclosure of
   Via1" is written `ENC V1 M1` (operand order opposite to intuition).
4. **Parenthesized layer booleans fail**: `(A NOT B)` and `(A AND B)` give
   `LAY7 invalid use of implicit layer definition`; bare `A NOT B` /
   `A OR B` compile. Derivations in the deck avoid parentheses.
5. **Comments**: both `//` and `*` line comments accepted.
6. Summary-report count lines have shape
   `RULECHECK <name> .... TOTAL Result Count = N (N)`; zero-count checks are
   listed when `Keep Empty Checks: YES` (default here).
7. Calibre exits rc=0 even with violations; rc≠0 means environment/deck/layout
   failure. The runner script distinguishes license failures (log regex) from
   deck errors before trying the next license candidate.

## 5. Automation traps hit during bring-up

* Calibre expands `$VAR` in INCLUDE/LAYOUT PATH but **not** LAYOUT PRIMARY
  (AGENTS.md §4). pvrun.py rewrites both statements itself
  (`rewrite_layout`) and pushes an instantiated copy
  (`sg13g2_beol_gen.drc`).
* `harness/common.py::vm_push` single-quotes the remote path, so `$HOME` and
  `~` would not expand there; pvrun resolves the VM home once (`vm_home`)
  and uses absolute paths everywhere.
* Top cell cannot be taken from the first STRNAME (file order ≠ hierarchy);
  pvrun walks STRNAME/SNAME records and picks the unreferenced structure
  (`chip_top_logo_fill`).

## 6. Wave-1 result vs magic baseline

Baseline of record: `LinHT-rfic/verification/drc/chip_top_logo_fill.magic.drc.rpt`
(snapshot copied to `reference/chip_top_logo_fill.magic.drc.rpt`),
content: `[INFO] COUNT: 0` — magic reports only an aggregate count, no
per-rule detail.

Calibre wave 1 (`evidence/wave1_drc/report.json`, real run on VM samjna,
license var above):

* 40 rule checks executed across 22 layers; ~4M flattened polygons seen
  (M1 1.10M, V1 418k instances, V2 242k, TV1 90k, ...).
* **Total violations: 0** — all 40 families clean.
* Delta vs magic: **agree** (magic 0 / calibre 0). The two tools overlap only
  partially: magic covers width/space-style geometry but has no equivalent of
  the via-size/enclosure table or the edge-seal-aware masking, so calibre is
  strictly stronger on this subset. Magic's known blind spots that this deck
  also does not cover: density, antenna, conditional wide-metal spacing
  (listed in §3 as out of scope).

Runtime: full-chip `-drc -hier` completes in ~25 s wall on the VM thanks to
hierarchical analysis (unique-geometry counts collapse 418k via instances to
17k). No turbo/flex licensing options were needed.

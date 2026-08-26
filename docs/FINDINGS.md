# FINDINGS — commercial-tool validation of the LinHT-rfic flow

Status date: 2026-08-26. Reference of record: the open-source flow itself
(ngspice-46 in `linht_iic`, magic/netgen DRC/LVS). Commercial cross-checks:
Keysight ADS hpeesofsim 650.shp (2027), Cadence Spectre 25.1, Siemens Calibre
2026.3_27.19 — all on VM `samjna`, all VPN-license-gated.

## Verdict matrix

| # | Stage | Case | Commercial tool | Verdict | Evidence |
|---|---|---|---|---|---|
| W1 | DRC (BEOL metal subset, 40 rule families) | chip_top_logo_fill.gds (~4M polys) | Calibre | **PASS — 0 violations, agrees with magic baseline** | `evidence/wave1_drc/report.json` |
| W3a | Circuit tran, lodiv chain, tt/4.4 GHz/div8 | full macro TB | hpeesofsim | **PASS — edge counts equal (28/28, 30/30, 16/16); median skew 35/22/4.6 ps; i_vdd mean Δ 0.26 %** | `evidence/lodiv_chain_tt_tt_ads_report.json` |
| W3b | Circuit tran, lodiv chain, ss/4.16 GHz edge | full macro TB | hpeesofsim | **FAIL — genuine parametric divergence**: divide topology identical (clk:lo ratio 0.49 both tools) but LO frequency differs ~6 % (ngspice 551 MHz vs ADS 519 MHz), per-edge median skew 0.33–0.57 ns | `evidence/lodiv_chain_ss_edge_ss_ads_report.json` |
| W2 | Circuit tran via Spectre | same TB | Spectre 25.1 | **IN PROGRESS — pipeline green through hierarchy elaboration and 33 ns tran (0 errors); DUT outputs static at mid-rail while sources oscillate — one remaining DUT-side debug item (see G-SPECTRE)** | `evidence/logs/lodiv_chain_tt_spectre.psfascii` |

## Toolflow-gap register (missing features this work exposed)

### G-CALIBRE — no Calibre deck ships with an open PDK (CLOSED by this repo)
IHP SG13G2 ships KLayout/Magic decks only. `calibre/sg13g2_beol.drc` is the
first Calibre path for this PDK: 40 checks transcribed from the KLayout Ruby
rule decks, every check annotated with its source rule id. License var
discovered empirically: `MGLS_LICENSE_FILE=1717@10.180.60.101`.
SVRF dialect facts verified on-VM (see `docs/calibre_deck_notes.md`):
no WIDTH/SPACE keywords (min-width=INT, spacing=EXT); brace-named checks;
ENC takes enclosed layer first; parenthesised layer booleans fail; calibre
exits rc=0 even WITH violations (parse the results DB, never trust rc).

### G-NETLIST-EXPORT — no commercial-dialect netlist export in the open flow (CLOSED)
xschem/ngspice emit ngspice-only decks. `harness/netadapt.py` converts to
hpeesofsim AND Spectre. Dialect facts encoded (all empirically proven):
- ADS: `*` comments rejected (use `;`); one top-level parameter per line;
  sources must be `V_Source:<name> n+ n- Vdc=v [V_Tran=<expr>]`;
  pulse maps to `pulse(time,V1,V2,TD,TR,TF,PW,PER)`; sine maps EXACTLY to
  `<amp>*sin(2*pi*<f>*time+(<ph>*pi/180))`; analyses `Tran:t StopTime= MaxTimeStep=`.
- ADS runs a **C preprocessor over netlists first**: a literal `/*` inside a
  comment silently swallows the rest of the file ("cpp: EOF in comment").
- Spectre: line 1 of ANY input file (including every included file) is the
  TITLE; `simulator lang=spectre` must follow it; extension governs default
  language of includes (stage `.spc`); bare intermediate assignments inside
  subckts are illegal (`parameters k=expr` lines instead); model-scope
  expressions cannot reference w/l/ng (strip to instance-level overrides);
  `ahdl_include psp103.va` SHADOWS Spectre's native `psp103` primitive and
  breaks setup (CMI-3078) — do not include the VA when using the built-in.
- Dataset extraction: hpeesofsim `-r raw` writes big-endian MDS whose layout
  we could not reverse reliably; **dsdump text is the robust interface**
  (dataset name follows TopDesignName, NOT the netlist stem).

### G-MODEL-FIDELITY — prior ADS wrapper simplification breaks ss-corner phase (OPEN)
The Aug-21 campaign's `ihp2ads.py` wrappers simplify IHP resistors
(rppd/rhigh as R+TC, "design-phase simplification of r3_cmc"). Consequence,
measured: at ss/4.16 GHz the lodiv chain's LO frequency differs ~6 % between
ngspice (OpenVAF-osdi models, full PDK resistor subckts) and ADS (VA PSP103 +
simplified R). Divide topology still agrees exactly. Remedy: regenerate ADS
wrappers mechanically from the PDK's own r3_cmc/resistor subckts (extension
point: `netadapt.gen_ads` + VM `~/linht/ads/pdk/sg13g2_<corner>.net`).
Until then: **ss-corner phase/delay sign-off must stay in ngspice/Xyce.**

### G-DRIFT — stale environment scripts rot silently
VM `~/linht/ads/sim.sh` pointed at `/opt/ads/ADS2025`, removed on upgrade.
Harnesses now generate their own env block (`ADS_ENV_TEXT`) pinned to
ADS2027. Lesson: pin tool paths in generated staging scripts, never source
hand-maintained ones.

### G-PSP-SCOPE — PSP103 model-card expressions are scope-sensitive
`iginvlw=(…l…w…)`, `cfrw=(…ng…)` etc. are legal in ngspice/ADS cards but
illegal in Spectre model scope (SFE-1999). netadapt strips them from cards;
wrappers would need instance-level equivalents — currently dropped entirely
(GIDL/fringe-cap second-order effects absent from Spectre runs).

## Harness inventory

| harness | purpose | self-test |
|---|---|---|
| `env_probe.py` | host/container/VM/license/tool/model verdicts (17 checks) | green |
| `netadapt.py` | ngspice → ADS/Spectre dialects + corner expansion + staging | green |
| `simrun.py` | three-simulator runners, parsers (wrdata/dsdump/psfascii), comparator families (edges/supply/waveforms) | green |
| `pvrun.py` | Calibre DRC runner/parser/magic-compare | green |

Comparator policy (config/cases.yaml): logic signals → rising-edge count
equality (±1 boundary tolerance) + median/max skew bounds + swing match;
supply rails → steady-state mean (±2 %) + envelope (±25 %); analog waveforms
→ smoothed L2. Pointwise L2 on square waves is explicitly rejected as a
primary metric (device-model phase drift accumulates ~20 ps/edge).

### G-SPECTRE — Spectre path state (W2 last mile)
Solved empirically en route: title-line-as-instance in included files,
`.spc` extension = compiled-table reader (use `.scs`), `parameters` inside
subckt bodies, model-scope geometry expressions, `sinedc` vs `dc`,
native `psp103` shadowed by ahdl_include of the VA.
CURRENT BLOCKER: full-macro Spectre tran completes 0-errors but DUT logic
outputs sit at ~VDD/2 while the sine source swings correctly (span 0.4 V
verified in psf) and rst_n releases on time. Supplies healthy. Next probe:
spectre DC operating point of the first CML stage (tail bias node) versus
ngspice — suspect a wrapper parameter not reaching depth (e.g. `m`
multiplicity ignored per SFE-57, or rhigh/rppd value semantics).
2. W4 LVS skeleton (Calibre LVS needs a device extraction deck — bigger
   transcription than DRC subset).
3. W5 Virtuoso stream-in audit.
4. G-MODEL-FIDELITY wrapper regeneration for ss-corner ADS sign-off.

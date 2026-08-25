# LinHT-rfic-validation

Commercial-tool validation of the [LinHT-rfic](https://github.com/Landflier/LinHT-rfic)
open-source RFIC flow — every stage of `Xschem → ngspice/CACE → LibreLane → GDS →
DRC/LVS` cross-checked against **Keysight ADS 2027**, **Cadence Virtuoso/Spectre
IC25.1/Spectre25.1**, and **Siemens Calibre 2026.3** on IHP SG13G2.

The harnesses here are deliberately reusable: they exist both to validate the chip
*and* to give the open-source flow entry points it is missing (notably a Calibre
rule-deck path for an open PDK, and dialect adapters from ngspice netlists to
Spectre/hpeesofsim).

## Layout

```
harness/      executable validation harnesses (Python 3.10+, stdlib only unless noted)
calibre/      SG13G2 Calibre rule decks authored by this project (metal DRC subset, LVS skeleton)
config/       tolerances and case definitions (YAML)
evidence/     per-wave run evidence (JSON reports, logs) — committed
docs/         FINDINGS.md gap register, runbooks
```

## Operating environment

| Where | What |
|---|---|
| Host `spectrumview` | orchestration, git, report generation |
| VM `samjna` (`ssh -p 2222 rakesh@127.0.0.1`) | all licensed tools: ADS 2027, Virtuoso IC25.1, Spectre 25.1, Calibre 2026.3_27.19; SG13G2 ADS corner models under `~/linht/ads/pdk/` |
| Container `linht_iic` | open-source reference flow: ngspice/Xyce/CACE/xschem/KLayout/Magic/netgen, PDK at `$PDK_ROOT/ihp-sg13g2` |

Licenses are VPN-gated (see `~/exp/rfic/cadence/start-cadence.sh status`).
`scp` is disabled on the VM — file transfer is `cat` over ssh.

## Quick start

```sh
python3 harness/env_probe.py            # environment verdicts (JSON)
python3 harness/netadapt.py --self-test # netlist adapter self-test
python3 harness/simrun.py --self-test   # simulator runners self-test
python3 harness/pvrun.py --self-test    # Calibre runner self-test
make waves                              # execute all validation waves
```

Every harness supports `--self-test`, exits nonzero on failure, emits JSON on
stdout, writes logs under `evidence/logs/`.

## Status

See `docs/FINDINGS.md` for the live PASS/FAIL matrix and toolflow-gap register.

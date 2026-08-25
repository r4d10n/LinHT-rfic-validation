# AGENTS.md — operating the LinHT-rfic validation harnesses

Manual for agents running or extending these validations. Read fully before
running jobs. Companion reading: `~/exp/sims/{ADS-AGENTS,CHIPS-AGENTS,CUA-AGENTS}.md`
(the operating manuals for the VM-side tools).

## 0. Rules that keep answers honest

1. **Never trust an exit code alone.** Every wave ends in a parsed-metric
   comparison against a reference (ngspice baseline or magic DRC report). A tool
   "running fine" with unparsed output is NOT a pass.
2. **The reference of record is the open-source flow itself**: fresh ngspice runs
   in `linht_iic`, magic/netgen DRC/LVS reports already committed in LinHT-rfic.
   Commercial tools are the cross-check.
3. **Tolerances come from `config/tolerances.yaml`.** Never inline a tolerance in
   a harness call; if a case needs a new tolerance, add it to YAML with a comment
   saying why.
4. **Evidence is committed.** Every wave writes `evidence/<wave>/*.{json,log}`
   and those files are part of the deliverable. No evidence, no done.
5. **No secrets in this repo.** The VM password lives only in host-side
   house-convention scripts outside the repo.

## 1. Where things run

| Stage | Runs on | Why |
|---|---|---|
| env_probe | host (+ssh probes) | fastest place to fail |
| netadapt | host | pure text transformation |
| simrun spectre / hpeesofsim | VM via ssh | licenses + models live there |
| pvrun calibre | VM via ssh | license + deck staging |
| reference ngspice | container `linht_iic` | open-source baseline |
| stream-in audit | VM (virtuoso -nograph) | GDS import check |

VM access pattern (from CUA-AGENTS.md §2): ssh with sshpass, port 2222, **no
scp/sftp — transfer with `cat` over ssh**; long jobs detached with nohup, polled
short; never hold a foreground ssh on a background pipe (tool timeout kills at
~2 min).

## 2. Adding a validation case

1. Add case definition to `config/cases.yaml`: name, macro, testbench paths,
   corner, analysis type, metrics, tolerances key.
2. Run `python3 harness/simrun.py --case <name>` — it will:
   - regenerate the ngspice reference in the container,
   - adapt netlists to both commercial dialects (`netadapt`),
   - run each simulator on the VM (detached, polled),
   - parse all three outputs into one metric dict per simulator,
   - emit `evidence/<case>/report.json` + `.md` via `report.py`.
3. Commit evidence + any tolerance change together.

## 3. Harness contract

- Python 3.10+ standard library only on the host; VM/container side scripts may
  use whatever the target image ships.
- `--self-test` must exercise the pure logic without needing VM/licenses, so CI
  can run it.
- JSON schemas for reports live in `harness/schemas.py`; validate before writing.
- Failures must name the exact stage that failed (`adapt:spectre`,
  `transfer:vm-push`, `run:hpeesofsim`, `parse:citi`, `compare:tolerance`).

## 4. Known traps (do not rediscover)

- VM `~/linht/ads/sim.sh` points at `/opt/ads/ADS2025` which is GONE — use the
  ADS2027 env block (ADS-AGENTS.md §3), including `$ADS/tools/python/lib` in
  LD_LIBRARY_PATH or bundled python dies with libpython missing.
- ADS netlists reject `*` comment lines; substrate/model types must be literal.
- Spectre schematic-dialect needs `scale=1e-6` when widths are in microns;
  PEX netlists carry explicit units — never scale those.
- Calibre expands `$VAR` in INCLUDE/LAYOUT PATH but not LAYOUT PRIMARY — expand
  templates yourself.
- hpeesofsim CITIfile: use the `S_50[...]` data set for renormalized S-params;
  parse block-per-DATA, complex re,im pairs.
- Licenses blip when the VPN tunnel re-keys: treat sudden license failure as
  "check tunnel" first (`start-cadence.sh status`).

## 5. Gap register discipline

Any missing feature discovered while validating goes to `docs/FINDINGS.md`
immediately, with: what the open-source flow lacks, what blocked you, and the
harness-based remedy (existing harness name or a new one to write). This register
is a primary deliverable, not an afterthought.

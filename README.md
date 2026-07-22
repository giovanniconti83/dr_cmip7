# CMCC CMIP7 output-variable selection

Turn the internal **CMCC CMIP7 data-request selection** into a concrete list of
model output variables — cross-checked against the official CMIP7 Data Request,
translated to the model's raw variable names, and sized in GB/model-year.

All the tooling lives in [`cmcc_selection/`](cmcc_selection/). This page is the
30-second guide; [`cmcc_selection/RUNBOOK.md`](cmcc_selection/RUNBOOK.md) has the
details and tuning knobs.

## Repo layout

| path | what it is |
|------|------------|
| `CMCC_CMIP7-DR-opportunities-Final*` | the internal CMCC selection (source of truth): opportunities, CMCC priority, variable groups |
| `cmcc_selection/` | our scripts + outputs |
| `CMIP7_DReq_Software_v1.4/` | upstream CMIP7 Data Request API (clone) |
| `cmip_reformatter/` | CMOR→raw-name lookup tables (clone; gitignored) |

## Setup (once, on the server)

Run the following
```bash
./downl_CMIP7_DReq.sh                    # script for cloning CMIP7_DReq_Software repository
conda env create -n my_dreq_env --file CMIP7_DReq_Software_v1.4/env.yml
conda activate my_dreq_env               # env with the DR API installed
pip install CMIP7-data-request-api       # if not already present
# CMOR -> raw-name lookup tables:
git clone https://github.com/CMCC-Foundation/cmip_reformatter.git   # inside dr_cmip7/
```

## Run it — 3 commands

```bash
cd cmcc_selection

# 1) select + expand + cross-check   -> the CMOR variable list
python build_cmcc_cmip7_table.py --version v1.2.2.4 --outdir out 2>/dev/null

# 2) translate to raw model names     -> the production list
python map_to_raw_names.py 2>/dev/null

# 3) estimate data volume             -> GB / model-year
python estimate_volume.py --version v1.2.2.4 2>/dev/null
```

(`2>/dev/null` just hides harmless `modeling_realm_-_primary` API warnings.)

## What you get (in `cmcc_selection/out/`)

| file | contents |
|------|----------|
| `cmcc_variables.csv` | **one row per unique CMOR variable** — compound name, `cmip6_name`, `out_name`, frequency, cell (ave/inst), realm, tier, and which groups/opportunities requested it |
| `cmcc_variables_mapped.csv` | same + `raw_name` and `in_reformatter` (True/False) columns |
| `cmcc_groups_crosscheck.csv` | **one row per requested group** — matched / aliased / unresolved, + variable count (the audit trail) |
| `by_realm/<realm>.csv` | per realm: `frequency \| cell \| n \| variables_dr` (DR names) |
| `raw/by_realm/<realm>.csv` | per realm: `variables_dr \| variables_raw \| variables_unmapped` — the production list |
| `raw/mapping_detail.csv` | every directly-mapped DR var → raw name, which lookup, `reprocess` flag |
| `raw/unmapped.csv` | **triage sheet** for vars with no direct raw mapping — see below |
| `volume_by_variable.csv` | per-variable GB/model-year, largest first |

### Mapping outcomes (three buckets)

Each selected variable ends up in exactly one bucket:

- **mapped** — its exact CMIP6 name is in the reformatter → produced directly
  (`tasmax→TREFHTMX`, `chlos→chl` with `reprocess`).
- **derivable** — the CMIP6 name is absent but its *base field* (the `out_name`)
  is in the reformatter → produce the base raw var and post-process
  (`thetao200` from `thetao`; `base_raw` column names it).
- **true_gap** — neither is known to the reformatter → not producible as-is
  (`co2s`, hemispheric sea-ice scalars, most `aerosol`/`atmosChem`).

`raw/unmapped.csv` lists the **derivable** and **true_gap** ones, sorted by
realm then category, with columns `realm | category | cmip6_name | out_name |
base_raw | frequency | cell | decision | …`. Fill the empty **`decision`**
column (`drop` / `add` / `derive`) with the colleague to resolve the gaps. The
run also prints a per-realm `mapped | derivable | true_gap` summary.

> **CMIP6 vs CMIP7 names.** CMIP7 uses *branded* variables: the daily max of
> `tas` has `out_name=tas` (+ `cell=max`), not `tasmax`. We therefore key both the
> display and the reformatter join on `cmip6_name` (`tasmax`, `mrsos`, …) — the
> name the reformatter lookups use — so no branded variable is hidden under or
> mis-mapped to its root.

## Do we have ALL the CMCC-requested variables?

The CMCC request is defined at the **variable-group** level, so "we have every
requested variable" ⇔ "every requested group was found in the DR and fully
expanded." Two things make that easy to trust:

**1. The build refuses to under-deliver.** `build_cmcc_cmip7_table.py` prints a
coverage line and **exits non-zero** if any requested group is left unresolved:

```
[check] 115 matched, 2 aliased, 0 unresolved
```

`matched` = group found verbatim in the DR · `aliased` = renamed group remapped
(see `ALIASES` in the script) · `unresolved` = a group we could NOT place → the
run aborts so it can never silently drop one. A clean run with `0 unresolved`
means **every group in every High/Medium opportunity was expanded in full.**

**2. One-command audit.** List anything that is not `matched`/`aliased` — it
should only be free-text notes that leaked from the CSV (e.g. a stray `not`),
never a real group:

```bash
cd cmcc_selection
python -c "
import csv, collections
rows = list(csv.DictReader(open('out/cmcc_groups_crosscheck.csv')))
print('status counts:', dict(collections.Counter(r['status'] for r in rows)))
leftover = [r for r in rows if r['status'] not in ('matched','aliased')]
print(f'{len(leftover)} not matched/aliased (expect only note fragments):')
for r in leftover:
    print('   ', r['status'], '|', r['group_requested'], '|', r['opportunity'])
"
```

**Optional sanity cross-check** against the CMCC file's own *total variables*
column — print our unique-variable count per opportunity and eyeball it (small
differences are expected: DR version drift + de-duplication of shared variables):

```bash
python -c "
import csv, collections
c = collections.Counter()
for r in csv.DictReader(open('out/cmcc_variables.csv')):
    for o in r['opportunities'].split(';'):
        c[o] += 1
for o, n in sorted(c.items()): print(f'{n:5d}  {o}')
"
```

> Note: variable *coverage* is guaranteed by group coverage above. `raw/unmapped.csv`
> is a **different** question — those variables ARE requested and present, they
> just don't yet have a raw-model translation (drop them, or extend the
> `cmip_reformatter` lookups).

## Key facts / decisions

- **Priority** = CMCC per-opportunity (High + Medium): all variables of every
  listed group, no per-variable DR-priority filtering. → 30 opportunities,
  ~118 group references, **853 unique variables**, each falling into one of the
  three mapping buckets above (see the run's per-realm summary / `unmapped.csv`).
- **DR version**: the CMCC file was authored against ~v1.2.2.2 but we build
  against **v1.2.2.4**; two renamed groups are remapped via `ALIASES`
  (`omip_geometry_physics → omip_scalars_high_priority`,
  `hydro_modelling_PET_daily → WaterResourcesPET_daily`).
- **Volume grid** (in `estimate_volume.py`): CMCC-ESM2 defaults — atmos 288×192
  L30, ocean 362×292 L50. Edit `HGRID`/`VLEV` for other configs.

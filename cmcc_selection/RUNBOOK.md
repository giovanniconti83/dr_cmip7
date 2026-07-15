# CMCC CMIP7 output-variable table — runbook

Turns the internal DR selection (`CMCC_CMIP7-DR-opportunities-Final`) into a
consolidated, per-realm list of CMIP7 output variables, cross-checked against the
official CMIP7 Data Request via `data_request_api`.

## Where to run
On the CMCC server / any env where `data_request_api` is pip-installed
(the laptop clone has the source but not the deps, so it can't load the DR).

```bash
conda activate <env-with-CMIP7-data-request-api>
cd dr_cmip7/cmcc_selection
```

## Step 1 — sanity-check the parse (no API needed)
```bash
python build_cmcc_cmip7_table.py --parse-only
# -> 30 High/Medium opportunities, 118 group references
```

## Step 2 — build the tables
```bash
python build_cmcc_cmip7_table.py --version v1.2.2.2 --outdir out
```
Version `v1.2.2.2` matches the internal file header (`DR-V1.2.2.2`).

Outputs in `out/`:
| file | contents |
|------|----------|
| `cmcc_groups_crosscheck.csv` | one row per requested group: matched / typo? / unmatched / ignored(note?), suggestion, tier, #vars |
| `cmcc_variables.csv`         | one row per **unique** variable: compound name, out_name, frequency, cell (ave/inst), realm, tier(s), source groups/opportunities |
| `by_realm/<realm>.csv`       | `frequency \| cell \| n \| variables` — the CMCC production-list shape |

**Read `cmcc_groups_crosscheck.csv` first** — every `typo?`/`unmatched` row is a
group name in the internal file that did not match the DR (rename, typo, or a
free-text note that leaked through the comma split, e.g. the `not…` fragment in
"Ocean Extremes"). Fix the internal file or accept, then re-run.

## Step 3 — data-volume estimate (separate, CMIP6 ref ≈ 100 GB / model-year)
The DR ships a volume estimator. Export the selected opportunities, edit the grid
sizes to CMCC's model, then estimate:
```bash
# opportunity-level export (ids from the crosscheck / airtable), then:
export_dreq_lists_json v1.2.2.2 cmcc_request.json -i <opp_ids> -p medium
estimate_dreq_volume v1.2.2.2            # writes size.yaml first run
#   edit size.yaml -> CMCC grid (nlon/nlat/nlev/...)
estimate_dreq_volume cmcc_request.json -o out/volume_estimate.json
```
`-p medium` keeps Core+High+Medium DR-priority variables (drops DR "Low").

## Step 4 — map to model raw names (cmip_reformatter)
`cmcc_variables.csv` / `by_realm/*.csv` carry **CMOR** `out_name`s. The final
"RELHUM, PS, T" raw-model form needs the CMOR→raw mapping from `cmip_reformatter`
(not in this repo — point the script at that mapping table when available).

## Tuning knobs
- `KEEP_PRIORITIES` in the script — currently `{high, medium}` (CMCC per-opportunity priority).
- `tier_of()` — parses `tier1/tier2` from group names for the tier split.
- `time_cell()` — collapses `cell_methods`/`temporal_shape` to `ave|inst|clim|max|min`.

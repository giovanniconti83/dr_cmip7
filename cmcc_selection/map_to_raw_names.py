#!/usr/bin/env python
"""
Map the CMOR variables selected by build_cmcc_cmip7_table.py to the model's RAW
output names, using the cmip_reformatter lookup tables
(cmip_reformatter/cmip-tables/cmip6plus/variables/*_lookup.csv).

Each lookup row is:  variable,reprocess,model,long_name
  variable = CMOR out_name (join key)   model = raw model variable name

Outputs (under <outdir>/raw/):
  by_realm/<realm>.csv   frequency | cell | n | variables   (RAW model names)
  mapping_detail.csv     every selected var -> raw name, lookup used, reprocess
  unmapped.csv           selected vars with NO reformatter entry (production gap)

No API needed - pure post-processing of out/cmcc_variables.csv.

Usage:
    python map_to_raw_names.py \
        --vars out/cmcc_variables.csv \
        --lookup-dir ../cmip_reformatter/cmip-tables/cmip6plus/variables \
        --outdir out
"""
import argparse
import csv
import os
from collections import defaultdict

# DR modeling_realm -> reformatter lookup family (file is <fam>_lookup.csv)
REALM_TO_LOOKUP = {
    "atmos": "atm", "aerosol": "atm", "atmosChem": "atm",
    "land": "lnd", "landIce": "lnd",
    "ocean": "ocn", "ocnBgchem": "ocnbgc",
    "seaIce": "ice",
}


def load_lookups(lookup_dir):
    """Return (per_fam, combined) where
    per_fam[fam][out_name] = dict(model, reprocess, long_name)
    combined[out_name]     = list of (fam, model)   (for fallback / audit)."""
    per_fam, combined = {}, defaultdict(list)
    for fam in sorted(set(REALM_TO_LOOKUP.values())):
        path = os.path.join(lookup_dir, f"{fam}_lookup.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        per_fam[fam] = {}
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                out = row["variable"].strip()
                rec = {"model": row["model"].strip(),
                       "reprocess": row.get("reprocess", "").strip(),
                       "long_name": row.get("long_name", "").strip()}
                per_fam[fam][out] = rec
                combined[out].append((fam, rec["model"]))
    return per_fam, combined


def resolve(out_name, realm, per_fam, combined):
    """Return (model, fam_used, reprocess) or (None, None, None) if unmapped."""
    fam = REALM_TO_LOOKUP.get(realm)
    if fam and out_name in per_fam[fam]:
        r = per_fam[fam][out_name]
        return r["model"], fam, r["reprocess"]
    # fallback: any lookup that has this out_name
    if out_name in combined:
        fam2, model = combined[out_name][0]
        r = per_fam[fam2][out_name]
        return r["model"], fam2 + "*", r["reprocess"]   # * = realm mismatch
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--vars", default=os.path.join(here, "out", "cmcc_variables.csv"))
    ap.add_argument("--lookup-dir",
                    default=os.path.join(here, "..", "cmip_reformatter",
                                         "cmip-tables", "cmip6plus", "variables"))
    ap.add_argument("--outdir", default=os.path.join(here, "out"))
    args = ap.parse_args()

    per_fam, combined = load_lookups(args.lookup_dir)
    n_lookup = sum(len(v) for v in per_fam.values())
    print(f"[lookup] {n_lookup} CMOR->raw entries across {len(per_fam)} families")

    with open(args.vars, newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"[vars]   {len(rows)} selected CMOR variables")

    raw_dir = os.path.join(args.outdir, "raw")
    realm_dir = os.path.join(raw_dir, "by_realm")
    os.makedirs(realm_dir, exist_ok=True)

    detail, unmapped = [], []
    by_realm = defaultdict(lambda: defaultdict(set))   # realm -> (freq,cell) -> {raw}
    per_realm_stat = defaultdict(lambda: [0, 0])       # realm -> [mapped, unmapped]

    for r in rows:
        out, realm = r["out_name"], r["realm"]
        freq, cell = r["frequency"], r["cell"]
        model, fam_used, reprocess = resolve(out, realm, per_fam, combined)
        if model is None:
            per_realm_stat[realm][1] += 1
            unmapped.append({"out_name": out, "realm": realm, "frequency": freq,
                             "cell": cell, "cmip6_table": r.get("cmip6_table", ""),
                             "long_name": r.get("long_name", ""),
                             "groups": r.get("groups", ""),
                             "opportunities": r.get("opportunities", "")})
            continue
        per_realm_stat[realm][0] += 1
        by_realm[realm][(freq, cell)].add(model)
        detail.append({"out_name": out, "model": model, "realm": realm,
                       "lookup": fam_used, "reprocess": reprocess,
                       "frequency": freq, "cell": cell,
                       "compound_name": r.get("compound_name", "")})

    # per-realm raw tables
    for realm, freqmap in sorted(by_realm.items()):
        p = os.path.join(realm_dir, f"{realm or 'unknown'}.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frequency", "cell", "n", "variables"])
            for (freq, cell), names in sorted(freqmap.items()):
                nm = sorted(names)
                w.writerow([freq, cell, len(nm), ", ".join(nm)])

    _write(os.path.join(raw_dir, "mapping_detail.csv"), detail,
           ["out_name", "model", "realm", "lookup", "reprocess",
            "frequency", "cell", "compound_name"])
    _write(os.path.join(raw_dir, "unmapped.csv"), unmapped,
           ["out_name", "realm", "frequency", "cell", "cmip6_table",
            "long_name", "groups", "opportunities"])

    tot_m = sum(s[0] for s in per_realm_stat.values())
    tot_u = sum(s[1] for s in per_realm_stat.values())
    print(f"[map]    {tot_m} mapped to raw names, {tot_u} unmapped")
    for realm in sorted(per_realm_stat):
        m, u = per_realm_stat[realm]
        print(f"           {realm:12s} mapped {m:4d}   unmapped {u:4d}")
    print(f"[write]  {realm_dir}/  (raw per-realm)")
    print(f"[write]  {os.path.join(raw_dir, 'mapping_detail.csv')}")
    print(f"[write]  {os.path.join(raw_dir, 'unmapped.csv')}  <- production gap, review this")


def _write(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()

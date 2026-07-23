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


def resolve(keys, realm, per_fam, combined):
    """keys = tuple of lookup keys to try in order (normally just the cmip6 name).
    Return (model, fam_used, reprocess) or (None, None, None) if unmapped."""
    fam = REALM_TO_LOOKUP.get(realm)
    # Look in the realm's own lookup family first...
    for key in keys:
        if key and fam and key in per_fam[fam]:
            r = per_fam[fam][key]
            return r["model"], fam, r["reprocess"]
    # ...then any lookup (realm mismatch, flagged with *)
    for key in keys:
        if key and key in combined:
            fam2, model = combined[key][0]
            r = per_fam[fam2][key]
            return r["model"], fam2 + "*", r["reprocess"]
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
    # realm -> (freq,cell) -> sets of dr/raw names + the two unmapped categories
    by_realm = defaultdict(lambda: defaultdict(
        lambda: {"dr": set(), "raw": set(), "derivable": set(), "true_gap": set()}))
    per_realm_stat = defaultdict(lambda: [0, 0])       # realm -> [mapped, unmapped]
    enriched = []                                      # cmcc_variables + raw columns

    for r in rows:
        out, realm = r.get("out_name", ""), r.get("realm", "")
        dr_name = r.get("cmip6_name") or out          # display / join key
        freq, cell = r.get("frequency", ""), r.get("cell", "")
        # Join on the exact CMIP6 name ONLY (out_name used only if there is no
        # cmip6 name). No fallback to the root out_name: mapping a derived var
        # (thetao200) onto its base field (thetao) would re-create the very
        # branded-variable conflation we fixed - such cases go to triage instead.
        model, fam_used, reprocess = resolve((dr_name,), realm, per_fam, combined)
        bucket = by_realm[realm][(freq, cell)]
        bucket["dr"].add(dr_name)
        row_out = dict(r)
        if model is None:
            per_realm_stat[realm][1] += 1
            row_out["raw_name"] = ""
            row_out["in_reformatter"] = "False"
            # Triage: is the base field (CMIP7 out_name) known to the reformatter
            # even though this derived cmip6_name isn't? If so it's DERIVABLE
            # (produce the base raw var, then slice/aggregate - 0 extra storage);
            # else a TRUE GAP (not produced at all).
            base = combined.get(out)
            if base:
                category, base_raw = "derivable", base[0][1]
            else:
                category, base_raw = "true_gap", ""
            row_out["map_category"] = category
            bucket[category].add(dr_name)
            unmapped.append({"realm": realm, "category": category,
                             "cmip6_name": dr_name, "out_name": out,
                             "base_raw": base_raw, "frequency": freq, "cell": cell,
                             "decision": "", "cmip6_table": r.get("cmip6_table", ""),
                             "long_name": r.get("long_name", ""),
                             "groups": r.get("groups", ""),
                             "opportunities": r.get("opportunities", "")})
        else:
            per_realm_stat[realm][0] += 1
            bucket["raw"].add(model)
            row_out["raw_name"] = model
            row_out["in_reformatter"] = "True"
            row_out["map_category"] = "mapped"
            detail.append({"cmip6_name": dr_name, "out_name": out, "model": model,
                           "realm": realm, "lookup": fam_used, "reprocess": reprocess,
                           "frequency": freq, "cell": cell,
                           "compound_name": r.get("compound_name", "")})
        enriched.append(row_out)

    # per-realm combined tables: DR names, raw names, and the two unmapped classes
    # (derivable = 0 extra storage; true_gap = would need adding to the model)
    for realm, freqmap in sorted(by_realm.items()):
        p = os.path.join(realm_dir, f"{realm or 'unknown'}.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frequency", "cell", "n_dr", "variables_dr", "variables_raw",
                        "variables_unmapped_derivable", "variables_unmapped_true_gap"])
            for (freq, cell), b in sorted(freqmap.items()):
                dr = sorted(x for x in b["dr"] if x)
                w.writerow([freq, cell, len(dr), ", ".join(dr),
                            ", ".join(sorted(b["raw"])),
                            ", ".join(sorted(b["derivable"])),
                            ", ".join(sorted(b["true_gap"]))])

    _write(os.path.join(raw_dir, "mapping_detail.csv"), detail,
           ["cmip6_name", "out_name", "model", "realm", "lookup", "reprocess",
            "frequency", "cell", "compound_name"])
    # unmapped = the triage sheet: realm | category | names | base_raw | decision
    unmapped.sort(key=lambda x: (x["realm"], x["category"] != "derivable", x["cmip6_name"]))
    _write(os.path.join(raw_dir, "unmapped.csv"), unmapped,
           ["realm", "category", "cmip6_name", "out_name", "base_raw",
            "frequency", "cell", "decision", "cmip6_table", "long_name",
            "groups", "opportunities"])
    # enriched per-variable master (cmcc_variables + raw_name + in_reformatter + category)
    if enriched:
        _write(os.path.join(args.outdir, "cmcc_variables_mapped.csv"), enriched,
               list(rows[0].keys()) + ["raw_name", "in_reformatter", "map_category"])

    tot_m = sum(s[0] for s in per_realm_stat.values())
    tot_u = sum(s[1] for s in per_realm_stat.values())
    n_deriv = sum(1 for u in unmapped if u["category"] == "derivable")
    n_gap = sum(1 for u in unmapped if u["category"] == "true_gap")
    print(f"[map]    {tot_m} mapped, {tot_u} unmapped "
          f"({n_deriv} derivable-from-base, {n_gap} true gaps)")
    print(f"{'realm':12s} {'mapped':>7s} {'derivable':>10s} {'true_gap':>9s}")
    for realm in sorted(per_realm_stat):
        m = per_realm_stat[realm][0]
        d = sum(1 for u in unmapped if u["realm"] == realm and u["category"] == "derivable")
        g = sum(1 for u in unmapped if u["realm"] == realm and u["category"] == "true_gap")
        print(f"{realm:12s} {m:7d} {d:10d} {g:9d}")
    print(f"[write]  {realm_dir}/  (per-realm: variables_dr | variables_raw | variables_unmapped)")
    print(f"[write]  {os.path.join(args.outdir, 'cmcc_variables_mapped.csv')}")
    print(f"[write]  {os.path.join(raw_dir, 'mapping_detail.csv')}")
    print(f"[write]  {os.path.join(raw_dir, 'unmapped.csv')}  <- TRIAGE SHEET "
          f"(realm|category|base_raw|decision); fill 'decision' = drop/add/derive")


def _write(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()

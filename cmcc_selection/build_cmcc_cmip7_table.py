#!/usr/bin/env python
"""
Build the CMCC CMIP7 output-variable table from the internal DR selection.

Pipeline (steps 1-5 of the CMCC roadmap; volume + reformatter are separate):

  1. Parse the internal request
       CMCC_CMIP7-DR-opportunities-Final - DR-Selection.csv
     keeping only opportunities flagged High or Medium CMCC priority, and
     collect the variable-group names listed for each.
  2. Load the CMIP7 Data Request via the API and expand every requested
     variable group into its variables (+ metadata: frequency, realm,
     time-cell ave/inst, out_name, compound name, ...).
  3. Cross-check: every requested group name is matched against the DR's
     actual group names; typos / renames / note-fragments are reported and
     never silently dropped.
  4. Split tier1 / tier2 (parsed from the group name) and de-duplicate
     variables that are requested by several groups/opportunities.
  5. Write:
       out/cmcc_groups_crosscheck.csv   - one row per requested group
       out/cmcc_variables.csv           - one row per (unique) variable
       out/by_realm/<realm>.csv         - frequency | cell | variables
                                          (the CMCC "6hr ave RELHUM,PS,T" shape,
                                           in CMOR names pending cmip_reformatter)

The API only runs where `data_request_api` is pip-installed (the CMCC server),
so run this there, not on a laptop without the package + deps.

Usage:
    python build_cmcc_cmip7_table.py \
        --csv "../CMCC_CMIP7-DR-opportunities-Final - DR-Selection.csv" \
        --version v1.2.2.2 \
        --outdir out
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict

# CMCC priorities we keep (per-opportunity priority from the internal file).
KEEP_PRIORITIES = {"high", "medium"}

# Group names in the CMCC file (authored ~v1.2.2.2) that are shorthand / were
# renamed and do not exist verbatim in the queried DR version. Map each to the
# actual DR group name(s) that reproduce its intended content.
#   - omip_geometry_physics  : OMIP was split in v1.2.2.4; geometry+physics
#     scalars now live in omip_scalars_high_priority. (Widen here to add
#     omip_vectors_high_priority / omip_parameterizations if desired.)
#   - hydro_modelling_PET_daily : redundant PET shorthand; its content is
#     already covered by WaterResourcesPET_daily (also requested in the same
#     opportunity), so this adds 0 net variables after de-dup.
ALIASES = {
    "omip_geometry_physics": ["omip_scalars_high_priority"],
    "hydro_modelling_PET_daily": ["WaterResourcesPET_daily"],
}


# --------------------------------------------------------------------------- #
# Step 1 - parse the internal CMCC selection CSV                              #
# --------------------------------------------------------------------------- #
def clean_group_token(tok):
    """Reduce a raw comma/newline-split token to a bare group id + a note.

    The internal file mixes real group names with free-text remarks, e.g.
      'DCPP_wider (only daily and monthly)'         -> ('DCPP_wider', 'only daily and monthly')
      'seaice_state_monthly_basic        '          -> ('seaice_state_monthly_basic', '')
      'Ocean_Temperature_Extremes. We request ...'  -> ('Ocean_Temperature_Extremes', 'We request ...')
    Returns (group_id, note) or (None, note) if nothing group-like is found.
    """
    tok = tok.strip()
    if not tok:
        return None, ""
    note = ""
    # pull off a parenthetical note
    m = re.search(r"\(([^)]*)\)", tok)
    if m:
        note = m.group(1).strip()
        tok = (tok[: m.start()] + tok[m.end():]).strip()
    # a group id is the leading [A-Za-z0-9_] run; anything after is a note
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(.*)$", tok)
    if not m:
        return None, (note + " " + tok).strip()
    gid, rest = m.group(1), m.group(2).strip(" .,")
    if rest:
        note = (note + " " + rest).strip()
    return gid, note


def parse_cmcc_csv(path):
    """Return list of dicts: {opportunity, priority, groups:[(gid,note)], raw}."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    # header row is the one containing 'CMCC priority'
    hdr_idx = next(
        (i for i, r in enumerate(rows) if any("CMCC priority" in c for c in r)),
        1,
    )
    out = []
    for r in rows[hdr_idx + 1:]:
        if len(r) < 5:
            continue
        opp, prio, vg = r[2].strip(), r[3].strip().lower(), r[4]
        if not opp or prio not in KEEP_PRIORITIES:
            continue
        groups, seen = [], set()
        for raw_tok in re.split(r"[,\n]", vg):
            gid, note = clean_group_token(raw_tok)
            if gid and gid.lower() not in seen:
                seen.add(gid.lower())
                groups.append((gid, note))
        out.append({"opportunity": opp, "priority": prio, "groups": groups, "raw": vg.strip()})
    return out


# --------------------------------------------------------------------------- #
# Step 2 - load the Data Request via the API                                  #
# --------------------------------------------------------------------------- #
def load_dr(version):
    """Return (DR object, {uid: metadata dict}, {normalized name: group obj})."""
    from data_request_api.content import dreq_content as dc
    from data_request_api.content import dump_transformation as dt
    from data_request_api.query import data_request as dr
    from data_request_api.query import dreq_query as dq

    # group membership (DR object)
    content_dic = dt.get_transformed_content(version=version)
    DR = dr.DataRequest.from_separated_inputs(**content_dic)

    # per-variable metadata, keyed by unique/compound name -> reindex by uid
    dc.retrieve(version)
    content = dc.load(version)
    meta_by_name = dq.get_variables_metadata(content, version, verbose=False)
    meta_by_name.pop("Header", None)
    meta_by_uid = {}
    for name, info in meta_by_name.items():
        info = dict(info)
        info["_unique_name"] = name
        meta_by_uid[info["uid"]] = info

    groups = {}
    for g in DR.get_variable_groups():
        nm = getattr(g, "name", None)
        nm = str(nm) if nm is not None else str(g)   # .name can be a ConstantValueObj
        groups[_norm(nm)] = (nm, g)
    return DR, meta_by_uid, groups


def _norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


# --------------------------------------------------------------------------- #
# Step 3/4 - cross-check + expand + tier + dedup                              #
# --------------------------------------------------------------------------- #
def tier_of(group_name):
    m = re.search(r"tier[_ ]?([0-9])", str(group_name), re.I)
    return f"tier{m.group(1)}" if m else ""


def time_cell(meta):
    """Collapse cell_methods / temporal_shape to a coarse ave|inst|clim|max|min."""
    cm = (meta.get("cell_methods") or "").lower()
    ts = (meta.get("temporal_shape") or "").lower()
    if "time: point" in cm or ts.endswith("point"):
        return "inst"
    if "maximum" in cm:
        return "max"
    if "minimum" in cm:
        return "min"
    if "clim" in ts or "climatology" in cm:
        return "clim"
    if "time:" in cm or ts.endswith("intv") or ts.startswith("time"):
        return "ave"
    return "unknown"


def build(selection, meta_by_uid, groups):
    import difflib

    dr_names = [v[0] for v in groups.values()]
    aliases_norm = {_norm(k): v for k, v in ALIASES.items()}
    crosscheck = []           # per requested group
    var_rows = {}             # unique_name -> record

    for entry in selection:
        opp, prio = entry["opportunity"], entry["priority"]
        for gid, note in entry["groups"]:
            key = _norm(gid)
            status, suggestion = "", ""
            targets = []                       # list of (real_name, group_obj)
            if key in groups:
                status = "matched"
                targets = [groups[key]]
            elif key in aliases_norm:
                status = "aliased"
                for real in aliases_norm[key]:
                    rk = _norm(real)
                    if rk in groups:
                        targets.append(groups[rk])
                    else:
                        status = "alias-broken"
                        suggestion = f"alias -> '{real}' not in DR"
            else:
                close = difflib.get_close_matches(gid, dr_names, n=1, cutoff=0.8)
                if close:
                    status, suggestion = "typo?", close[0]
                elif "_" in gid or re.search(r"[a-z][A-Z]", gid):
                    status = "unmatched"
                else:
                    status = "ignored(note?)"

            n_vars = 0
            for matched_name, gobj in targets:
                for var in gobj.get_variables():
                    uid = getattr(var, "uid", None)
                    meta = meta_by_uid.get(uid)
                    if meta is None:
                        continue
                    n_vars += 1
                    name = meta["_unique_name"]
                    rec = var_rows.get(name)
                    if rec is None:
                        c6c = meta.get("cmip6_compound_name", "") or ""
                        rec = {
                            "compound_name": name,
                            "out_name": meta.get("out_name", ""),
                            # CMIP6 short name (part after ".") - the identifier that
                            # disambiguates branded vars (tas tmax -> tasmax) and is the
                            # key the cmip_reformatter lookups are built on.
                            "cmip6_name": c6c.split(".")[-1] if c6c else "",
                            "cmip6_compound_name": c6c,
                            "frequency": meta.get("frequency", ""),
                            "realm": (meta.get("modeling_realm", "") or "").split(" ")[0],
                            "realm_all": meta.get("modeling_realm", ""),
                            "cell": time_cell(meta),
                            "cell_methods": meta.get("cell_methods", ""),
                            "cmip6_table": meta.get("cmip6_table", ""),
                            "long_name": meta.get("long_name", ""),
                            "tiers": set(),
                            "groups": set(),
                            "opportunities": set(),
                            "priorities": set(),
                        }
                        var_rows[name] = rec
                    if tier_of(matched_name):
                        rec["tiers"].add(tier_of(matched_name))
                    rec["groups"].add(matched_name)
                    rec["opportunities"].add(opp)
                    rec["priorities"].add(prio)

            crosscheck.append({
                "opportunity": opp,
                "cmcc_priority": prio,
                "group_requested": gid,
                "status": status,
                "matched_group": ";".join(n for n, _ in targets),
                "suggestion": suggestion,
                "tier": tier_of(";".join(n for n, _ in targets) or gid),
                "n_variables": n_vars,
                "note": note,
            })
    return crosscheck, var_rows


# --------------------------------------------------------------------------- #
# Step 5 - write outputs                                                       #
# --------------------------------------------------------------------------- #
def write_outputs(crosscheck, var_rows, outdir):
    os.makedirs(outdir, exist_ok=True)

    cc_path = os.path.join(outdir, "cmcc_groups_crosscheck.csv")
    with open(cc_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(crosscheck[0].keys()))
        w.writeheader()
        w.writerows(crosscheck)

    var_path = os.path.join(outdir, "cmcc_variables.csv")
    fields = ["compound_name", "cmip6_name", "out_name", "cmip6_compound_name",
              "frequency", "cell", "realm", "realm_all", "cmip6_table",
              "long_name", "tiers", "groups", "opportunities", "priorities",
              "cell_methods"]
    rows = sorted(var_rows.values(),
                  key=lambda r: (r["realm"], r["frequency"], r["cmip6_name"] or r["out_name"]))
    with open(var_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in rows:
            w.writerow([
                r["compound_name"], r["cmip6_name"], r["out_name"],
                r["cmip6_compound_name"], r["frequency"], r["cell"],
                r["realm"], r["realm_all"], r["cmip6_table"], r["long_name"],
                ";".join(sorted(r["tiers"])), ";".join(sorted(r["groups"])),
                ";".join(sorted(r["opportunities"])), ";".join(sorted(r["priorities"])),
                r["cell_methods"],
            ])

    # per-realm: frequency | cell | variables_dr (CMCC production-list shape).
    # Display the CMIP6 name (falls back to out_name) so branded vars are visible
    # as themselves (tasmax, mrsos), not collapsed into their root (tas, mrsol).
    realm_dir = os.path.join(outdir, "by_realm")
    os.makedirs(realm_dir, exist_ok=True)
    by_realm = defaultdict(lambda: defaultdict(set))   # realm -> (freq,cell) -> {dr_name}
    for r in rows:
        dr_name = r["cmip6_name"] or r["out_name"]
        by_realm[r["realm"] or "unknown"][(r["frequency"], r["cell"])].add(dr_name)
    for realm, freqmap in sorted(by_realm.items()):
        p = os.path.join(realm_dir, f"{realm or 'unknown'}.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frequency", "cell", "n", "variables_dr"])
            for (freq, cell), names in sorted(freqmap.items()):
                nm = sorted(n for n in names if n)
                w.writerow([freq, cell, len(nm), ", ".join(nm)])
    return cc_path, var_path, realm_dir


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--csv",
                    default=os.path.join(here, "..",
                                         "CMCC_CMIP7-DR-opportunities-Final - DR-Selection.csv"))
    ap.add_argument("--version", default="v1.2.2.2")
    ap.add_argument("--outdir", default=os.path.join(here, "out"))
    ap.add_argument("--parse-only", action="store_true",
                    help="only parse the CMCC CSV and print the selection (no API needed)")
    ap.add_argument("--allow-unmatched", action="store_true",
                    help="do not exit non-zero when some groups stay unresolved")
    args = ap.parse_args()

    selection = parse_cmcc_csv(args.csv)
    n_groups = sum(len(e["groups"]) for e in selection)
    print(f"[parse] {len(selection)} High/Medium opportunities, "
          f"{n_groups} group references")
    if args.parse_only:
        for e in selection:
            print(f"  [{e['priority']:6}] {e['opportunity']}")
            for gid, note in e["groups"]:
                print(f"        - {gid}" + (f"   # {note}" if note else ""))
        return

    print(f"[load] loading Data Request {args.version} via API ...")
    _DR, meta_by_uid, groups = load_dr(args.version)
    print(f"[load] {len(groups)} variable groups, {len(meta_by_uid)} variables in DR")

    crosscheck, var_rows = build(selection, meta_by_uid, groups)

    matched = sum(1 for c in crosscheck if c["status"] == "matched")
    aliased = [c for c in crosscheck if c["status"] == "aliased"]
    fatal = [c for c in crosscheck if c["status"] in ("typo?", "unmatched", "alias-broken")]
    print(f"[check] {matched} matched, {len(aliased)} aliased, {len(fatal)} unresolved")
    for c in aliased:
        print(f"        [alias] {c['group_requested']} -> {c['matched_group']}"
              f"  (+{c['n_variables']} vars)")
    for c in fatal:
        extra = f" -> {c['suggestion']}" if c["suggestion"] else ""
        print(f"        [{c['status']}] {c['group_requested']}"
              f"  ({c['opportunity']}){extra}")
    if fatal and not args.allow_unmatched:
        print("\n[abort] unresolved groups above would be silently dropped. "
              "Add them to ALIASES (or rerun with --allow-unmatched).",
              file=sys.stderr)
        sys.exit(1)

    cc, var, realm_dir = write_outputs(crosscheck, var_rows, args.outdir)
    print(f"[write] {len(var_rows)} unique variables")
    print(f"[write] {cc}")
    print(f"[write] {var}")
    print(f"[write] per-realm tables in {realm_dir}/")
    print("\nNext (separate steps):")
    print("  * data volume:  estimate_dreq_volume <request.json>  "
          "(CMIP6 ref ~100 GB / model-year)")
    print("  * raw names:    map out_name -> model raw vars via cmip_reformatter")


if __name__ == "__main__":
    main()

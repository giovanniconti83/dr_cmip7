#!/usr/bin/env python
"""
Suggest v-to-v variable-group renames by UID overlap.

For each 'old' group name (as written in the CMCC file, resolved against an
old DR version), find which group(s) in a new DR version contain the same
variables (matched by stable record UID). Prints coverage so the mapping is
evidence-based, not a fuzzy-name guess.

Usage:
    python suggest_group_mapping.py \
        --old-version v1.2.2.2 --new-version v1.2.2.4 \
        --groups omip_geometry_physics,hydro_modelling_PET_daily
"""
import argparse
import re


def norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


def load_groups(version):
    from data_request_api.content import dump_transformation as dt
    from data_request_api.query import data_request as dr
    DR = dr.DataRequest.from_separated_inputs(**dt.get_transformed_content(version=version))
    groups = {}
    for g in DR.get_variable_groups():
        nm = str(getattr(g, "name", g))
        uids = {getattr(v, "uid", None) for v in g.get_variables()}
        uids.discard(None)
        groups[norm(nm)] = (nm, uids)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-version", default="v1.2.2.2")
    ap.add_argument("--new-version", default="v1.2.2.4")
    ap.add_argument("--groups", required=True, help="comma-separated old group names")
    args = ap.parse_args()

    print(f"[load] old {args.old_version} ...")
    old = load_groups(args.old_version)
    print(f"[load] new {args.new_version} ...")
    new = load_groups(args.new_version)

    for raw in args.groups.split(","):
        raw = raw.strip()
        key = norm(raw)
        print("\n" + "=" * 70)
        if key not in old:
            print(f"{raw}: NOT FOUND in {args.old_version} either -> can't compare")
            continue
        oname, ouids = old[key]
        print(f"{oname}  ({len(ouids)} vars in {args.old_version})")
        # rank new groups by how much of the old set they cover
        ranked = []
        for nname, nuids in new.values():
            inter = ouids & nuids
            if inter:
                ranked.append((len(inter), len(inter) / len(ouids), nname, len(nuids)))
        ranked.sort(reverse=True)
        if not ranked:
            print("  no v-new group shares any variable (fully removed/replaced)")
            continue
        print(f"  candidate {args.new_version} groups (by shared vars):")
        covered = set()
        for n_inter, frac, nname, nsize in ranked[:8]:
            print(f"    {frac*100:5.1f}%  {n_inter:3d}/{len(ouids)} shared   "
                  f"{nname}  (group has {nsize})")
        # minimal set to fully cover, greedily
        remaining = set(ouids)
        chosen = []
        pool = {}
        for nname, nuids in new.values():
            ov = ouids & nuids
            if ov:
                pool[nname] = ov
        while remaining and pool:
            best = max(pool, key=lambda k: len(pool[k] & remaining))
            gain = pool[best] & remaining
            if not gain:
                break
            chosen.append((best, len(gain)))
            remaining -= gain
            del pool[best]
        cov = len(ouids) - len(remaining)
        print(f"  -> minimal cover ({cov}/{len(ouids)} = {cov/len(ouids)*100:.0f}%): "
              + ", ".join(f"{n}(+{g})" for n, g in chosen))
        if remaining:
            print(f"     {len(remaining)} old vars not covered by any new group")


if __name__ == "__main__":
    main()

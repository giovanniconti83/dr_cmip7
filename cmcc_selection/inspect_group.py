#!/usr/bin/env python
"""
List variable groups (matching a substring) and their variables, for one DR
version. Use it to see what candidate groups actually contain so a rename /
alias can be chosen by intent.

Usage:
    python inspect_group.py --version v1.2.2.4 --match omip
    python inspect_group.py --version v1.2.2.4 --match hydro,water,pet
"""
import argparse
import re


def norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1.2.2.4")
    ap.add_argument("--match", required=True, help="comma-separated substrings (case-insensitive)")
    args = ap.parse_args()
    pats = [p.strip().lower() for p in args.match.split(",") if p.strip()]

    from data_request_api.content import dreq_content as dc
    from data_request_api.content import dump_transformation as dt
    from data_request_api.query import data_request as dr
    from data_request_api.query import dreq_query as dq

    DR = dr.DataRequest.from_separated_inputs(**dt.get_transformed_content(version=args.version))
    dc.retrieve(args.version)
    meta = dq.get_variables_metadata(dc.load(args.version), args.version, verbose=False)
    meta.pop("Header", None)
    by_uid = {info["uid"]: info for info in meta.values()}

    for g in sorted(DR.get_variable_groups(), key=lambda x: str(getattr(x, "name", x))):
        nm = str(getattr(g, "name", g))
        if not any(p in nm.lower() for p in pats):
            continue
        vs = g.get_variables()
        print(f"\n### {nm}   [{len(vs)} vars]")
        print(f"    {'out_name':16s} {'cmip6_name':16s} {'freq':6s} {'cell_methods'}")
        rows = []
        for v in vs:
            info = by_uid.get(getattr(v, "uid", None), {})
            c6 = (info.get("cmip6_compound_name", "") or "").split(".")[-1]
            rows.append((info.get("out_name", "?"), c6,
                         info.get("frequency", "?"),
                         info.get("cell_methods", "")))
        for out, c6, freq, cm in sorted(rows):
            print(f"    {out:16s} {c6:16s} {freq:6s} {cm}")


if __name__ == "__main__":
    main()

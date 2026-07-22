#!/usr/bin/env python
"""
Estimate data volume (GB / model-year) for the CMCC selection.

Reuses the CMIP7 DR size math (get_variable_size, dimension sizes) but:
  * applies a PER-REALM horizontal grid (the stock estimate_dreq_volume uses a
    single longitude/latitude for every realm, which is wrong for atmos vs ocean),
  * totals over OUR exact selection (out/cmcc_variables.csv),
  * reports request (all selected) vs producible (mapped by cmip_reformatter), and
  * writes a GB_per_year column into cmcc_variables_mapped.csv.

Grid = CMCC-ESM3, calibrated from a real B1850 run
(/work/cmcc/pf28319/CMCC-ESM3/archive/B1850DEVLTbc.26):
  * atmos/land: CAM/CLM spectral-element grid, ncol = 48600 columns, 58 levels
  * ocean/ice:  NEMO grid 360 x 291 = 104760 points, 75 depth levels
  * all fields float32; raw model output is ~uncompressed (measured ratio ~1.0).
    Set --compression to model the archived (deflated) size.
Edit HGRID / VLEV below for other configs.

Usage:
    python estimate_volume.py --version v1.2.2.4 \
        --vars out/cmcc_variables.csv --mapped out/raw/mapping_detail.csv
"""
import argparse
import copy
import csv
import os
from collections import defaultdict

# Per-realm-family horizontal grid (nlon, nlat). atmos/land use an unstructured
# SE grid, represented as (ncol, 1) so longitude*latitude = ncol.
HGRID = {"atmos": (48600, 1), "ocean": (360, 291)}
# atmos/ocean vertical levels enter via dimension names alevel/olevel
VLEV = {"alevel": 58, "alevhalf": 59, "olevel": 75, "olevhalf": 75}
REALM_FAMILY = {
    "atmos": "atmos", "aerosol": "atmos", "atmosChem": "atmos",
    "land": "atmos", "landIce": "atmos",
    "ocean": "ocean", "ocnBgchem": "ocean", "seaIce": "ocean",
}

DAYS_PER_YEAR = 365
FREQ_TIMES_PER_YEAR = {
    "subhr": DAYS_PER_YEAR * 48, "1hr": DAYS_PER_YEAR * 24,
    "3hr": DAYS_PER_YEAR * 8, "6hr": DAYS_PER_YEAR * 4,
    "day": DAYS_PER_YEAR, "mon": 12, "yr": 1, "dec": 0.1, "fx": 1,
}
GB = 1024 ** 3


def get_variable_size(var_info, dreq_dim_sizes, time_dims, config):
    """Size (bytes) of 1 year of a variable. Copied from estimate_dreq_volume.py."""
    dimensions = var_info["dimensions"]
    if isinstance(dimensions, str):
        dimensions = dimensions.split()
    dim_sizes = {}
    for dim in dimensions:
        if dim in time_dims:
            frequency = var_info["frequency"]
            if dim == "diurnal-cycle":
                n = 24 * 12
            else:
                n = FREQ_TIMES_PER_YEAR[frequency]
        elif dim in config["dimensions"]:
            n = config["dimensions"][dim]
        else:
            n = dreq_dim_sizes[dim]
        if n is None:
            raise ValueError(f"No size for dimension: {dim}")
        dim_sizes[dim] = n
    num = 1
    for dim in dim_sizes:
        num *= dim_sizes[dim]
    return num * config["bytes_per_float"] * config["scale_file_size"]


def base_config(compression=1.0):
    dims = {"gridlatitude": 291, "latitude": 291, "longitude": 360,
            "rho": 75, "sdepth": 20, "soilpools": 5, "spectband": 10}
    dims.update(VLEV)
    return {"dimensions": dims, "bytes_per_float": 4, "scale_file_size": compression}


def realm_config(base, realm):
    fam = REALM_FAMILY.get(realm, "atmos")
    nlon, nlat = HGRID[fam]
    cfg = copy.deepcopy(base)
    cfg["dimensions"]["longitude"] = nlon
    cfg["dimensions"]["latitude"] = nlat
    return cfg


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--version", default="v1.2.2.4")
    ap.add_argument("--vars", default=os.path.join(here, "out", "cmcc_variables.csv"))
    ap.add_argument("--mapped", default=os.path.join(here, "out", "raw", "mapping_detail.csv"))
    ap.add_argument("--outdir", default=os.path.join(here, "out"))
    ap.add_argument("--compression", type=float, default=1.0,
                    help="on-disk/raw ratio (1.0 = uncompressed, as the raw run; "
                         "use e.g. 0.5 to model deflated archive size)")
    args = ap.parse_args()

    import data_request_api.content.dreq_content as dc
    import data_request_api.query.dreq_query as dq

    # selection
    with open(args.vars, newline="") as f:
        sel = list(csv.DictReader(f))
    sel_names = [r["compound_name"] for r in sel]
    mapped_names = set()
    if os.path.exists(args.mapped):
        with open(args.mapped, newline="") as f:
            mapped_names = {r["compound_name"] for r in csv.DictReader(f) if r.get("compound_name")}
    print(f"[vars] {len(sel_names)} selected, {len(mapped_names)} producible (mapped)")

    # DR size machinery
    dc.retrieve(args.version)
    content = dc.load(args.version)
    base = dq.create_dreq_tables_for_request(content, args.version)
    dreq_tables = {
        "coordinates and dimensions": base["Coordinates and Dimensions"],
        "temporal shape": base["Temporal Shape"],
        "frequency": base["CMIP7 Frequency"],
        "spatial shape": base["Spatial Shape"],
    }
    dreq_dim_sizes = dq.get_dimension_sizes(dreq_tables)
    time_dims = {}
    for rec in dreq_tables["temporal shape"].records.values():
        if hasattr(rec, "dimensions"):
            link = rec.dimensions[0]
            dim_name = dreq_tables["coordinates and dimensions"].get_record(link).name
        else:
            dim_name = "None"
        time_dims[dim_name] = rec.name

    meta = dq.get_variables_metadata(base, args.version, compound_names=sel_names)
    meta.pop("Header", None)

    cfg0 = base_config(args.compression)
    # aggregate bytes/year
    per_realm = defaultdict(lambda: [0.0, 0.0])       # realm -> [request, producible]
    per_freq = defaultdict(lambda: [0.0, 0.0])
    total = [0.0, 0.0]
    detail = []
    gb_by_name = {}                                   # compound_name -> GB/year
    for name in sel_names:
        info = meta.get(name)
        if info is None:
            continue
        realm = (info.get("modeling_realm", "") or "atmos").split(" ")[0]
        freq = info.get("frequency", "")
        size = get_variable_size(info, dreq_dim_sizes, time_dims, realm_config(cfg0, realm))
        gb_by_name[name] = size / GB
        prod = name in mapped_names
        per_realm[realm][0] += size
        per_freq[freq][0] += size
        total[0] += size
        if prod:
            per_realm[realm][1] += size
            per_freq[freq][1] += size
            total[1] += size
        detail.append((name, realm, freq, size / GB, prod))

    def gb(x):
        return f"{x/GB:8.2f}"

    print(f"\n=== GB / model-year (compression={args.compression}) ===   request   producible")
    print(f"{'TOTAL':22s}          {gb(total[0])}   {gb(total[1])}")
    print("\n--- by realm ---")
    for r in sorted(per_realm, key=lambda k: -per_realm[k][0]):
        print(f"{r:22s}          {gb(per_realm[r][0])}   {gb(per_realm[r][1])}")
    print("\n--- by frequency ---")
    for fr in sorted(per_freq, key=lambda k: -per_freq[k][0]):
        print(f"{fr:22s}          {gb(per_freq[fr][0])}   {gb(per_freq[fr][1])}")
    print("\n(ref: CMIP6 ~100 GB/model-year. Provisional - CMCC-ESM3 grid; "
          "raw model output ~uncompressed, pass --compression for archived size.)")

    out = os.path.join(args.outdir, "volume_by_variable.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["compound_name", "realm", "frequency", "GB_per_year", "producible"])
        for name, realm, freq, gbyr, prod in sorted(detail, key=lambda x: -x[3]):
            w.writerow([name, realm, freq, f"{gbyr:.4f}", prod])
    print(f"\n[write] {out} (per-variable, largest first)")

    # merge a GB_per_year column into cmcc_variables_mapped.csv (colleague's ask)
    mapped_csv = os.path.join(args.outdir, "cmcc_variables_mapped.csv")
    if os.path.exists(mapped_csv):
        with open(mapped_csv, newline="") as f:
            mrows = list(csv.DictReader(f))
        fields = list(mrows[0].keys())
        if "GB_per_year" not in fields:
            fields.append("GB_per_year")
        for r in mrows:
            r["GB_per_year"] = f"{gb_by_name.get(r['compound_name'], 0.0):.4f}"
        with open(mapped_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(mrows)
        print(f"[write] {mapped_csv}  (added GB_per_year column)")


if __name__ == "__main__":
    main()

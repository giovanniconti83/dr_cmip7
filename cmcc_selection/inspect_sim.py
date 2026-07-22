#!/usr/bin/env python
"""
Inspect a CESM-style model archive to calibrate the volume estimate with REAL
numbers: per-component grid sizes, 2D/3D variable counts, timesteps per file,
number of files per history stream, and the on-disk compression ratio.

The example run need not contain all requested variables - we only need each
component's grid + compression, which apply to every variable on that grid.

Usage:
    python inspect_sim.py /work/cmcc/pf28319/CMCC-ESM3/archive/B1850DEVLTbc.26

Run it in an env with netCDF4 (e.g. my_dreq_env). If netCDF4 is missing it
still reports file sizes/streams and tells you the ncdump command to run.
"""
import glob
import os
import re
import sys
from collections import defaultdict

try:
    import netCDF4
except ImportError:
    netCDF4 = None

COMPONENTS = ["atm", "ocn", "ice", "lnd", "rof", "cpl", "esp"]
# strip a trailing CESM date stamp: .YYYY-MM[-DD[-SSSSS]].nc
DATE_RE = re.compile(r"\.\d{4}-\d{2}(-\d{2})?(-\d{5})?\.nc$")


def human(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python inspect_sim.py <archive_dir>")
    root = sys.argv[1]
    print(f"archive: {root}")
    print(f"netCDF4: {'available' if netCDF4 else 'MISSING (headers not read)'}")

    for comp in COMPONENTS:
        hist = os.path.join(root, comp, "hist")
        if not os.path.isdir(hist):
            continue
        files = sorted(glob.glob(os.path.join(hist, "*.nc")))
        if not files:
            print(f"\n##### {comp}/hist : no .nc files")
            continue
        streams = defaultdict(list)
        for f in files:
            streams[DATE_RE.sub("", os.path.basename(f))].append(f)
        print(f"\n##### {comp}/hist : {len(files)} files, {len(streams)} stream(s)")

        for st, fs in sorted(streams.items()):
            sizes = [os.path.getsize(x) for x in fs]
            first = os.path.basename(fs[0])
            last = os.path.basename(fs[-1])
            print(f"\n  [{st}]  files={len(fs)}  avg={human(sum(sizes)/len(fs))}  "
                  f"total={human(sum(sizes))}")
            print(f"    first={first}  last={last}")
            if netCDF4 is None:
                print(f"    -> ncdump -h '{fs[0]}'")
                continue
            try:
                ds = netCDF4.Dataset(fs[0])
            except Exception as e:  # noqa: BLE001
                print(f"    open failed: {e}")
                continue
            dims = {d: len(ds.dimensions[d]) for d in ds.dimensions}
            raw, nvar = 0, 0
            by_ndim = defaultdict(int)
            samples = []
            for vn, v in ds.variables.items():
                if vn in ds.dimensions:
                    continue
                nvar += 1
                by_ndim[v.ndim] += 1
                npts = 1
                for d in v.dimensions:
                    npts *= len(ds.dimensions[d])
                raw += npts * v.dtype.itemsize
                if len(samples) < 8:
                    samples.append(f"{vn}{tuple(v.dimensions)}")
            tsteps = len(ds.dimensions["time"]) if "time" in ds.dimensions else 1
            print("    dims: " + ", ".join(f"{k}={v}" for k, v in dims.items()))
            print(f"    data_vars={nvar}  by_ndim={dict(by_ndim)}  timesteps/file={tsteps}")
            if raw:
                print(f"    on-disk {human(sizes[0])} vs raw {human(raw)}  "
                      f"-> compression ratio {sizes[0]/raw:.3f}")
            print("    e.g. " + "; ".join(samples))
            ds.close()


if __name__ == "__main__":
    main()

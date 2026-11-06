#!/usr/bin/env python3
"""
SimScale solid-temperature extractor - steady-state solid temperature field from a finished SimScale CHT run.

Downloads the OpenFOAM results of a finished conjugate-heat-transfer (CHT) run, reads the temperature field of
every SOLID region at the last (steady-state) time step, and writes one table per solid region:

    t,X,Y,Z,Temperature value C

ready to be used as an initial / applied temperature field in a SimScale thermomechanical simulation (the solid
geometry of the CHT run and the thermomechanical run must match). This tool only reads solid-region temperatures -
no heat transfer coefficient is computed (see the sibling tool SimScale_HTC_Extractor for that).

Usage (Windows: use "py" instead of "python3"):
    python3 simscale_solid_temperature_extractor.py --run <simscale workbench link to a finished run>
    python3 simscale_solid_temperature_extractor.py --run <link of a simulation, latest finished run is used>
    python3 simscale_solid_temperature_extractor.py --zip C:\\path\\results.zip
    python3 simscale_solid_temperature_extractor.py --run <link> --dry-run

The API key is read from --api-key, else the SIMSCALE_API_KEY environment variable, else asked for.
Run with --help for all options.
"""
import argparse, getpass, gzip, json, os, re, shutil, sys, time, urllib.error, urllib.request, zipfile
from pathlib import Path

import numpy as np

API = "https://api.simscale.com/v0"


# ----------------------------------------------------------------------------------------- SimScale API
def log(*a):
    print(*a, flush=True)


def api_get(path, key, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API + path, headers={"X-API-KEY": key})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit("SimScale API refused the key (HTTP %d). Check the API key." % e.code)
            if e.code == 404 or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def parse_link(link):
    """-> (project_id, kind, id) from a Workbench link, e.g.
    https://www.simscale.com/workbench/?pid=123&location=simulationRun:<sim uuid>:<run uuid>  (kind = 'simrun')
    https://www.simscale.com/workbench/?pid=123&location=simulationRun:<run uuid>             (kind = 'run')
    https://www.simscale.com/workbench/?pid=123&location=simulation:<uuid>                    (kind = 'simulation')"""
    link = urllib.request.unquote(link.strip())
    pid = re.search(r"pid=(\d+)", link)
    both = re.search(r"simulationRun:([0-9a-f-]{36}):([0-9a-f-]{36})", link)
    run = re.search(r"simulationRun:([0-9a-f-]{36})", link)
    sim = re.search(r"simulation:([0-9a-f-]{36})", link)
    if not pid or not (run or sim):
        sys.exit("Could not read a project id and a simulation/run id from the link:\n  " + link)
    if both:
        return pid.group(1), ("simrun", both.groups())
    return pid.group(1), ("run", run.group(1)) if run else ("simulation", sim.group(1))


def resolve_run(key, pid, kind, ident):
    """-> (simulation_id, run_id, run_name). For a simulation link the latest FINISHED run is used."""
    if kind == "simrun":
        sid, rid = ident
        r = api_get(f"/projects/{pid}/simulations/{sid}/runs/{rid}", key)
        if r.get("status") != "FINISHED":
            sys.exit(f"Run {rid} has status {r.get('status')}, not FINISHED.")
        return sid, rid, r.get("name", rid)
    if kind == "run":
        rid = ident
        sims = api_get(f"/projects/{pid}/simulations?limit=500", key).get("_embedded", [])
        # the run id alone does not tell us the simulation id; ask every simulation for this run
        for s in sims:
            sid = s["simulationId"]
            try:
                r = api_get(f"/projects/{pid}/simulations/{sid}/runs/{rid}", key)
            except Exception:
                continue
            if r:
                if r.get("status") != "FINISHED":
                    sys.exit(f"Run {rid} has status {r.get('status')}, not FINISHED.")
                return sid, rid, r.get("name", rid)
        sys.exit(f"Run {rid} was not found in project {pid}.")
    sid = ident
    runs = api_get(f"/projects/{pid}/simulations/{sid}/runs?limit=500", key).get("_embedded", [])
    finished = [r for r in runs if r.get("status") == "FINISHED"]
    if not finished:
        sys.exit(f"Simulation {sid} has no finished run.")
    r = max(finished, key=lambda r: r.get("endTime", ""))
    return sid, r["runId"], r.get("name", r["runId"])


def solution_url(key, pid, sid, rid):
    res = api_get(f"/projects/{pid}/simulations/{sid}/runs/{rid}/results?limit=500", key).get("_embedded", [])
    sol = [r for r in res if r.get("type") == "SOLUTION_FIELD"]
    if not sol:
        sys.exit("The run has no SOLUTION_FIELD result to download.")
    d = sol[0]["download"]
    return d["url"], d.get("uncompressedSizeInBytes")


def download(url, key, dest, retries=6):
    """Stream the archive to disk. SimScale builds the zip on the fly, so a broken transfer restarts."""
    for attempt in range(1, retries + 1):
        n, t0 = 0, time.time()
        try:
            req = urllib.request.Request(url, headers={"X-API-KEY": key})
            with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
                while True:
                    b = r.read(8 << 20)
                    if not b:
                        break
                    f.write(b)
                    n += len(b)
                    rate = n / max(time.time() - t0, 1e-3) / 1e6
                    print(f"\r  downloaded {n / 1e9:6.2f} GB  ({rate:5.1f} MB/s)", end="", flush=True)
            print()
            if not zipfile.is_zipfile(dest):
                raise IOError("downloaded file is not a valid zip")
            return
        except Exception as e:
            print()
            log(f"  download attempt {attempt} failed after {n / 1e6:.0f} MB: {type(e).__name__}: {e}")
            if attempt == retries:
                sys.exit("Download failed. A stable network connection is needed for these large archives.")
            time.sleep(20)


# ----------------------------------------------------------------------------------------- archive handling
def opener(path):
    return gzip.open(path, "rt") if str(path).endswith(".gz") else open(path)


def extract_needed(zip_path, out_dir):
    """Extract the mesh and the T field of the last time step for every SOLID region. Returns
    (case_dir, time_name, [region names])."""
    out_dir = Path(out_dir)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        nameset = set(names)
        times = {n.split("/")[0] for n in names if re.fullmatch(r"[0-9.eE+-]+", n.split("/")[0] or "x")}
        times = sorted(times, key=float)
        if not times:
            sys.exit("No time directories found in the archive.")
        t = times[-1]

        def has(field, reg):
            pre = f"{t}/{reg}/" if reg else f"{t}/"
            return (pre + field) in nameset or (pre + field + ".gz") in nameset

        cand = sorted({n.split("/")[1] for n in names if n.startswith(t + "/") and n.count("/") == 2})
        if cand:
            # multi-region case: a region is a SOLID if it has T but no U (no flow)
            regs = [r for r in cand if has("T", r) and not has("U", r)]
            if not regs:
                sys.exit("No solid region (a region with a temperature field and no velocity field) found. "
                         "Is this a CHT run with at least one solid body?")
        else:
            # single-region case: fields directly under the time directory - only valid if it is a pure solid run
            if has("T", "") and not has("U", ""):
                regs = [""]
            else:
                sys.exit("Only one region was found and it has a velocity field, i.e. it is fluid, not solid. "
                         "This tool reads solid-region temperatures from a multi-region CHT run.")

        want = []
        for n in names:
            parts = n.split("/")
            for reg in regs:
                mesh = f"constant/{reg}/polyMesh/" if reg else "constant/polyMesh/"
                fld = f"{t}/{reg}/" if reg else f"{t}/"
                if n.startswith(mesh) and not n.endswith("/"):
                    want.append(n)
                elif n.startswith(fld) and re.search(r"/T(\.gz)?$", n) and n.count("/") == fld.count("/"):
                    want.append(n)
            if parts[0] == "system" and parts[-1] == "controlDict":
                want.append(n)
            if n in ("constant/regionProperties",):
                want.append(n)
        total = sum(z.getinfo(n).file_size for n in want)
        log(f"  extracting {len(want)} files ({total / 1e9:.2f} GB) for time {t}, solid regions {regs or ['(single)']}")
        for n in want:
            z.extract(n, out_dir)
    (out_dir / "case.foam").touch()
    return out_dir, t, regs


def read_labels(path):
    with opener(path) as f:
        for line in f:
            if line.strip() == "(":
                break
        text = f.read()
    text = text[:text.rindex(")")]
    return np.array(text.split(), dtype=np.int64)


def n_cells_of(case_dir, region):
    mesh = Path(case_dir) / "constant" / region / "polyMesh" if region else Path(case_dir) / "constant" / "polyMesh"
    for f in ("owner", "owner.gz"):
        p = mesh / f
        if p.exists():
            owner = read_labels(p)
            break
    else:
        sys.exit(f"owner file not found for region '{region}'")
    return int(owner.max()) + 1


def read_cells_vtk(case_dir, region, time_name, n_cells):
    """Cell centres and cell temperature of a region via VTK's OpenFOAM reader."""
    from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader
    from vtkmodules.vtkFiltersCore import vtkCellCenters
    from vtkmodules.vtkCommonDataModel import vtkCompositeDataSet
    from vtkmodules.util.numpy_support import vtk_to_numpy
    r = vtkOpenFOAMReader()
    r.SetFileName(str(Path(case_dir) / "case.foam"))
    r.SetCreateCellToPoint(0)
    r.UpdateInformation()
    r.DisableAllPatchArrays()
    r.SetPatchArrayStatus(f"/{region}/internalMesh" if region else "internalMesh", 1)
    for i in range(r.GetNumberOfCellArrays()):
        nm = r.GetCellArrayName(i)
        r.SetCellArrayStatus(nm, 1 if nm == "T" else 0)
    r.SetTimeValue(float(time_name))
    r.Update()
    top = r.GetOutput()

    def first_grid(b):
        if b is None:
            return None
        if b.IsA("vtkUnstructuredGrid"):
            return b
        for i in range(b.GetNumberOfBlocks()):
            g = first_grid(b.GetBlock(i))
            if g is not None and g.GetNumberOfCells() > 0:
                return g
        return None

    grid = None
    if region:
        for i in range(top.GetNumberOfBlocks()):
            if top.GetMetaData(i).Get(vtkCompositeDataSet.NAME()) == region:
                grid = first_grid(top.GetBlock(i))
    else:
        grid = first_grid(top)
    if grid is None or grid.GetNumberOfCells() != n_cells:
        sys.exit(f"VTK could not read the internal mesh of region '{region}' consistently.")
    cc = vtkCellCenters()
    cc.SetInputData(grid)
    cc.Update()
    return (vtk_to_numpy(cc.GetOutput().GetPoints().GetData()).copy(),
            vtk_to_numpy(grid.GetCellData().GetArray("T")).copy())


# ----------------------------------------------------------------------------------------- reduction
def coarsen(xyz, T, target):
    """Merge cells into voxels so that about `target` positions remain (plain mean per voxel)."""
    if target <= 0 or len(xyz) <= target:
        return xyz, T

    def merge(vox):
        key = np.floor((xyz - xyz.min(0)) / vox).astype(np.int64)
        _, inv = np.unique(key, axis=0, return_inverse=True)
        inv = inv.ravel()
        n = inv.max() + 1
        cnt = np.bincount(inv, minlength=n)
        out_xyz = np.column_stack([np.bincount(inv, xyz[:, k], n) / cnt for k in range(3)])
        out_T = np.bincount(inv, T, n) / cnt
        return out_xyz, out_T

    lo, hi = 1e-4, 1.0
    for _ in range(40):
        mid = (lo * hi) ** 0.5
        if len(merge(mid)[0]) > target:
            lo = mid
        else:
            hi = mid
    return merge(hi)


def write_table(name, xyz, T_K, out_dir):
    o = np.lexsort((xyz[:, 2], xyz[:, 1], xyz[:, 0]))
    X, Y, Z, Tc = xyz[o, 0], xyz[o, 1], xyz[o, 2], T_K[o] - 273.15
    rows = [f"0,{X[i]:.9g},{Y[i]:.9g},{Z[i]:.9g},{Tc[i]:.6g}" for i in range(len(X))]
    (out_dir / f"{name}_temperature.csv").write_text(
        "t,X,Y,Z,Temperature value C\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)


# ----------------------------------------------------------------------------------------- main
def selftest():
    """Check that all compiled dependencies load."""
    from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader
    from vtkmodules.vtkFiltersCore import vtkCellCenters
    from vtkmodules.vtkCommonCore import vtkVersion, vtkDoubleArray
    from vtkmodules.vtkCommonDataModel import vtkCompositeDataSet
    from vtkmodules.util.numpy_support import vtk_to_numpy
    vtkOpenFOAMReader(); vtkCellCenters(); vtkCompositeDataSet.NAME()
    a = vtkDoubleArray(); a.InsertNextValue(1.0); assert vtk_to_numpy(a)[0] == 1.0
    log(f"selftest OK: python {sys.version.split()[0]}, numpy {np.__version__}, vtk {vtkVersion.GetVTKVersion()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", metavar="LINK", help="SimScale Workbench link of a finished run (or simulation)")
    ap.add_argument("--zip", metavar="PATH", help="use an already downloaded result zip instead of downloading")
    ap.add_argument("--api-key", help="SimScale API key (else SIMSCALE_API_KEY, else prompt)")
    ap.add_argument("--out", default="solid_temperatures", help="output folder (default: solid_temperatures)")
    ap.add_argument("--positions", type=int, default=20000,
                    help="target number of positions per table (default 20000; 0 = keep every cell)")
    ap.add_argument("--keep-download", action="store_true", help="keep the downloaded zip and the extracted files")
    ap.add_argument("--dry-run", action="store_true", help="only resolve the run and show the download size")
    ap.add_argument("--selftest", action="store_true", help="check that all dependencies load, then exit")
    opt = ap.parse_args(argv)
    if opt.selftest:
        selftest()
        return
    if not opt.run and not opt.zip:
        ap.print_help()
        sys.exit("\nGive --run <link> or --zip <path>.")
    if opt.run and opt.zip:
        sys.exit("Give either --run or --zip, not both.")

    out = Path(opt.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)

    if opt.zip:
        zpath = Path(opt.zip)
    else:
        key = opt.api_key or os.environ.get("SIMSCALE_API_KEY") or getpass.getpass("SimScale API key: ").strip()
        pid, (kind, ident) = parse_link(opt.run)
        sid, rid, rname = resolve_run(key, pid, kind, ident)
        url, size = solution_url(key, pid, sid, rid)
        log(f"project {pid}, simulation {sid}, run {rid} ('{rname}'), solution ~{(size or 0) / 1e9:.1f} GB")
        if opt.dry_run:
            return
        zpath = work / "solution.zip"
        download(url, key, zpath)

    case, tname, regs = extract_needed(zpath, work / "case")
    log(f"time step {tname} (steady-state result)")
    for reg in regs:
        name = reg or "solid"
        ncell = n_cells_of(case, reg)
        log(f"  region '{name}': {ncell} cells, reading temperature field ...")
        xyz, T_K = read_cells_vtk(case, reg, tname, ncell)
        xyz_r, T_r = coarsen(xyz, T_K, opt.positions)
        nrow = write_table(name, xyz_r, T_r, out)
        log(f"  {name}: {nrow} positions written, mean temperature "
            f"{float(np.mean(T_K)) - 273.15:.1f} degC, range "
            f"{float(np.min(T_K)) - 273.15:.1f} .. {float(np.max(T_K)) - 273.15:.1f} degC")

    if not opt.keep_download:
        shutil.rmtree(work / "case", ignore_errors=True)
        if opt.run:
            zpath.unlink(missing_ok=True)

    log(f"\ndone. Tables written to {out}")


if __name__ == "__main__":
    main()

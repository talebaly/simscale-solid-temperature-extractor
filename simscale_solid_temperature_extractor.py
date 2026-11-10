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


def download(url, key, dest, retries=60, prepare_retries=40, prepare_wait=45):
    """Stream the archive to disk. SimScale builds the zip on the fly, so a broken transfer resumes where it left off.

    SimScale prepares large CHT result exports on demand; while that is in progress the download URL answers
    HTTP 404 ("Downloadable result objects not found"). This is expected and can take a while for a big mesh, so
    a 404 is retried patiently (default: every 45 s for up to 30 min, Ctrl+C / Cancel to give up sooner) instead
    of being treated as a network hiccup. Network errors (timeouts, resets) get the same patience - a huge
    transfer over a flaky connection needs far more than a couple of quick retries - and resume via HTTP Range
    from the bytes already on disk instead of restarting from zero."""
    net_attempt, prep_attempt = 0, 0
    dest = Path(dest)
    while True:
        resume_from = dest.stat().st_size if dest.exists() else 0
        n, t0 = resume_from, time.time()
        try:
            headers = {"X-API-KEY": key}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=900) as r:
                resumed = resume_from and r.status == 206
                with open(dest, "ab" if resumed else "wb") as f:
                    if resume_from and not resumed:
                        n = 0  # server ignored the Range request - restarting from zero
                    while True:
                        b = r.read(8 << 20)
                        if not b:
                            break
                        f.write(b)
                        n += len(b)
                        rate = (n - resume_from) / max(time.time() - t0, 1e-3) / 1e6
                        print(f"\r  downloaded {n / 1e9:6.2f} GB  ({rate:5.1f} MB/s)", end="", flush=True)
            print()
            if not zipfile.is_zipfile(dest):
                dest.unlink(missing_ok=True)  # stale/corrupt partial - next attempt must restart from zero
                raise IOError("downloaded file is not a valid zip")
            return
        except urllib.error.HTTPError as e:
            print()
            if e.code in (401, 403):
                reason = "the API key was refused" if e.code == 401 else "access was refused"
                sys.exit(f"Download failed (HTTP {e.code}): {reason}.")
            if e.code == 404:
                # The export may have been invalidated/rebuilt server-side - a partial file on disk is no longer
                # guaranteed to match, so drop it rather than risk resuming onto bytes from a different build.
                dest.unlink(missing_ok=True)
                prep_attempt += 1
                if prep_attempt > prepare_retries:
                    sys.exit(f"Download failed (HTTP 404) after waiting about "
                             f"{prepare_retries * prepare_wait // 60} min: SimScale still has no downloadable "
                             "result for this run. Large CHT exports are built on demand and can take a while - "
                             "check in the Workbench whether the manual download has finished preparing there, "
                             "or failed.")
                log(f"  SimScale is still preparing this result (HTTP 404; large CHT exports can take a while) - "
                    f"waiting {prepare_wait}s (check {prep_attempt}/{prepare_retries}) ...")
                time.sleep(prepare_wait)
                continue
            net_attempt += 1
            log(f"  download attempt {net_attempt} failed after {n / 1e6:.0f} MB: HTTP {e.code}")
            if net_attempt >= retries:
                sys.exit("Download failed. A stable network connection is needed for these large archives.")
            time.sleep(20)
        except Exception as e:
            print()
            net_attempt += 1
            log(f"  download attempt {net_attempt} failed after {n / 1e6:.0f} MB: {type(e).__name__}: {e}")
            if net_attempt >= retries:
                sys.exit("Download failed. A stable network connection is needed for these large archives.")
            time.sleep(20)


# ----------------------------------------------------------------------------------------- archive handling
def opener(path):
    return gzip.open(path, "rt") if str(path).endswith(".gz") else open(path)


def find(path):
    for p in (str(path), str(path) + ".gz"):
        if os.path.exists(p):
            return p
    return None


def parse_cht_region_names(txt):
    """-> (fluid_names, solid_names) as sets, from constant/chtRegionDict - SimScale's own record of which
    region/zone names are solid vs fluid. Used in preference to guessing from field presence."""
    fm = re.search(r"fluidRegionNames\s*\(([^)]*)\)", txt, re.S)
    sm = re.search(r"solidRegionNames\s*\(([^)]*)\)", txt, re.S)
    if not sm:
        return None, None
    return (set(fm.group(1).split()) if fm else set()), set(sm.group(1).split())


def parse_cell_zones(path):
    """-> {zone_name: np.ndarray of cell labels} from constant/polyMesh/cellZones."""
    txt = opener(path).read()
    pat = re.compile(r"([A-Za-z0-9_][\w.\-]*)\s*\{\s*type\s+cellZone;\s*cellLabels\s+List<label>\s*(\d+)\s*"
                     r"\((.*?)\)\s*;\s*\}", re.S)
    zones = {}
    for m in pat.finditer(txt):
        n = int(m.group(2))
        labels = np.array(m.group(3).split(), dtype=np.int64)
        if len(labels) != n:
            sys.exit(f"cellZones: zone '{m.group(1)}' declares {n} cells but {len(labels)} were read.")
        zones[m.group(1)] = labels
    return zones


def extract_needed(zip_path, out_dir):
    """Extract the mesh and the T field of the last time step needed to get SOLID-region temperatures. Returns
    (case_dir, time_name, info) where info is one of:
      {"mode": "region", "regions": [names]}                         - one or more solid region directories
      {"mode": "zones", "region": name, "solid_zone_names": {names}} - one mesh, solid cells selected by cellZone

    Which region/zone names are solid is read from constant/chtRegionDict (SimScale always writes this for a CHT
    run) where available; the heuristic "has a temperature field but no velocity field" is used as a fallback
    only when that file is missing."""
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

        cht_txt = None
        for cand_name in ("constant/chtRegionDict", "constant/chtRegionDict.gz"):
            if cand_name in nameset:
                with z.open(cand_name) as f:
                    raw = f.read()
                cht_txt = gzip.decompress(raw).decode() if cand_name.endswith(".gz") else raw.decode()
                break
        _, cht_solid_names = parse_cht_region_names(cht_txt) if cht_txt else (None, None)

        # OpenFOAM writes non-region bookkeeping folders directly under the time directory too
        not_a_region = {"uniform", "lagrangian", "polyMesh"}
        cand = sorted({n.split("/")[1] for n in names if n.startswith(t + "/") and n.count("/") == 2}
                      - not_a_region)
        info = None
        if cand:
            # multi-region case: one subregion directory per solid/fluid region
            if cht_solid_names:
                regs = [r for r in cand if r in cht_solid_names]
                if not regs:
                    log("  WARNING: chtRegionDict's solidRegionNames do not match any region directory; "
                        "falling back to the T-without-U heuristic")
            if not cht_solid_names or not regs:
                regs = [r for r in cand if has("T", r) and not has("U", r)]
            if not regs:
                sys.exit("No solid region (a region with a temperature field and no velocity field) found. "
                         "Is this a CHT run with at least one solid body?")
            info = {"mode": "region", "regions": regs}
            fld_regs = regs
        else:
            # single-mesh case: fields directly under the time directory
            if not has("T", ""):
                sys.exit("No temperature field (T) found at the last time step.")
            if cht_solid_names:
                # solid/fluid split is by constant/polyMesh/cellZones, not by region directory
                info = {"mode": "zones", "region": "", "solid_zone_names": cht_solid_names}
                fld_regs = [""]
            elif not has("U", ""):
                info = {"mode": "region", "regions": [""]}
                fld_regs = [""]
            else:
                sys.exit("Only one region was found and it has a velocity field, i.e. it is fluid, not solid, "
                         "and no constant/chtRegionDict was found to split it by solid/fluid cell zones.")

        want = []
        for n in names:
            parts = n.split("/")
            for reg in fld_regs:
                mesh = f"constant/{reg}/polyMesh/" if reg else "constant/polyMesh/"
                fld = f"{t}/{reg}/" if reg else f"{t}/"
                if n.startswith(mesh) and not n.endswith("/"):
                    want.append(n)
                elif n.startswith(fld) and re.search(r"/T(\.gz)?$", n) and n.count("/") == fld.count("/"):
                    want.append(n)
            if parts[0] == "system" and parts[-1] == "controlDict":
                want.append(n)
            if n in ("constant/regionProperties", "constant/chtRegionDict", "constant/chtRegionDict.gz"):
                want.append(n)
        if info["mode"] == "zones":
            want += [n for n in ("constant/polyMesh/cellZones", "constant/polyMesh/cellZones.gz") if n in nameset]
        total = sum(z.getinfo(n).file_size for n in want)
        label = info["regions"] if info["mode"] == "region" else ["(cell zones)"]
        log(f"  extracting {len(want)} files ({total / 1e9:.2f} GB) for time {t}, solid {label}")
        for n in want:
            z.extract(n, out_dir)
    (out_dir / "case.foam").touch()
    return out_dir, t, info


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

    case, tname, info = extract_needed(zpath, work / "case")
    log(f"time step {tname} (steady-state result)")

    def report_and_write(name, xyz, T_K):
        xyz_r, T_r = coarsen(xyz, T_K, opt.positions)
        nrow = write_table(name, xyz_r, T_r, out)
        log(f"  {name}: {nrow} positions written, mean temperature "
            f"{float(np.mean(T_K)) - 273.15:.1f} degC, range "
            f"{float(np.min(T_K)) - 273.15:.1f} .. {float(np.max(T_K)) - 273.15:.1f} degC")

    if info["mode"] == "region":
        for reg in info["regions"]:
            name = reg or "solid"
            ncell = n_cells_of(case, reg)
            log(f"  region '{name}': {ncell} cells, reading temperature field ...")
            xyz, T_K = read_cells_vtk(case, reg, tname, ncell)
            report_and_write(name, xyz, T_K)
    else:
        # single mesh (fluid and solid share one region); solid cells are selected via constant/polyMesh/cellZones
        reg = info["region"]
        ncell = n_cells_of(case, reg)
        log(f"  mesh: {ncell} cells total, reading temperature field ...")
        xyz, T_K = read_cells_vtk(case, reg, tname, ncell)
        cz_path = find(Path(case) / "constant" / reg / "polyMesh" / "cellZones" if reg
                        else Path(case) / "constant" / "polyMesh" / "cellZones")
        if not cz_path:
            sys.exit("constant/polyMesh/cellZones not found; cannot separate solid from fluid cells.")
        zones = parse_cell_zones(cz_path)
        missing = info["solid_zone_names"] - zones.keys()
        if missing:
            log(f"  WARNING: {len(missing)} solid zone name(s) from chtRegionDict have no matching cellZone: "
                f"{sorted(missing)[:5]}{' ...' if len(missing) > 5 else ''}")
        solid_zones = info["solid_zone_names"] & zones.keys()
        if not solid_zones:
            sys.exit("None of chtRegionDict's solidRegionNames match a cellZone in this mesh.")
        idx = np.unique(np.concatenate([zones[n] for n in solid_zones]))
        if idx.max(initial=-1) >= ncell:
            sys.exit("A cellZone references a cell index outside the mesh; cell numbering did not match.")
        log(f"  {len(solid_zones)} solid cell zones, {len(idx)} of {ncell} cells "
            f"({len(idx) / ncell:.0%}) are solid")
        report_and_write("solid", xyz[idx], T_K[idx])

    if not opt.keep_download:
        shutil.rmtree(work / "case", ignore_errors=True)
        if opt.run:
            zpath.unlink(missing_ok=True)

    log(f"\ndone. Tables written to {out}")


if __name__ == "__main__":
    main()

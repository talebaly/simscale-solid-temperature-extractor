# SimScale Solid Temperature Extractor

Reads the **steady-state solid temperature field** from a finished SimScale conjugate-heat-transfer (CHT) run and
writes one table per solid region, ready to use as an initial / applied temperature field in a SimScale
thermomechanical simulation on the same geometry.

```
SimScale CHT run (steady state) ──► download results ──► solid-region temperature, last time step
                                 ──► table  t, X, Y, Z, Temperature value C  per solid region
```

This is the simplified sibling of [`SimScale_HTC_Extractor`](../SimScale_HTC_Extractor): no heat transfer
coefficient, no mid-channel walk, no time-varying cool-down modes - just the solid temperatures from one steady
CHT solution. Use `SimScale_HTC_Extractor` instead when thin channels/sheets make a full convective boundary
condition (HTC + reference temperature) necessary.

## 1. Installation

**Windows, no Python needed:** download the build artifact *SimScaleSolidTemperatureExtractor-windows* from this
repo's GitHub Actions run, unzip it; the programs are `dist/SimScaleSolidTemperatureExtractor.exe` (window) and
`dist/simscale_solid_temperature_extractor_cli.exe` (command line). The files are not code-signed, so Windows
SmartScreen may ask for confirmation the first time ("More info" → "Run anyway").

**Any system with Python 3.9–3.12 (Windows, macOS, Linux):**

```
pip install -r requirements.txt           (Windows: py -m pip install -r requirements.txt)
python3 solid_temperature_extractor_gui.py        (window)
python3 simscale_solid_temperature_extractor.py --help   (command line)
```

You need a SimScale API key (avatar menu → **User Settings** → **API Keys** → **Generate Key**), a few GB of free
disk space while the result is processed, and a stable network connection.

## 2. Using the window

Three fields: paste your **API key**, paste the **SimScale Workbench link** of the finished CHT run (or choose an
already-downloaded result zip instead), pick an **output folder**, click **Extract temperatures**. The log shows
progress; **Open output folder** reveals the CSV files when done.

## 3. Command line

```
python3 simscale_solid_temperature_extractor.py --run "<SimScale Workbench link>"
python3 simscale_solid_temperature_extractor.py --run "<link>" --dry-run      # only resolve the run, no download
python3 simscale_solid_temperature_extractor.py --zip "C:\Downloads\results.zip"
```

The link can point to a finished **run** (`…location=simulationRun:<run uuid>`), a **specific run inside a
simulation** (`…location=simulationRun:<sim uuid>:<run uuid>`), or a **simulation**
(`…location=simulation:<uuid>`, in which case the latest finished run is used). The run must have status
`FINISHED` - a failed, canceled or still-running job cannot be downloaded.

The API key comes from `--api-key`, else the `SIMSCALE_API_KEY` environment variable, else you are asked
(hidden input).

## 4. Output

One file per solid region in the CHT case, `<region>_temperature.csv`:

| Column | Meaning | Unit |
|---|---|---|
| `t` | always `0` (steady state, single time layer) | s |
| `X, Y, Z` | cell-centre position | m |
| `Temperature value C` | steady-state solid temperature | °C |

In the thermomechanical simulation, use the table as the value source for a temperature boundary condition /
initial condition on the corresponding solid body (independent variables X, Y, Z; the value is the last column).
If a solid region's table is too large for your mesh / UI, reduce `--positions` (voxel-averaged to roughly that
many points; default 20000, `0` keeps every mesh cell).

## 5. How solid regions are found

A SimScale CHT case is split into regions. This tool extracts the last time step and treats a region as **solid**
if it has a temperature field (`T`) but no velocity field (`U`) - i.e. no flow is solved there. Regions with both
`T` and `U` (the fluid regions) are skipped.

## 6. Options

| Option | Default | Meaning |
|---|---|---|
| `--out` | `solid_temperatures` | Output folder |
| `--positions` | `20000` | Target number of positions per table (voxel-binned mean); `0` = every cell |
| `--keep-download` | off | Keep the downloaded zip and extracted files (several GB) |
| `--dry-run` | off | Only resolve the run and print the result size |
| `--selftest` | - | Check that numpy/vtk load, then exit |

## 7. Limitations

* Reads ASCII OpenFOAM CHT results only (SimScale default for Code_Aster-fed CHT cases).
* One steady-state time layer only - no cool-down / warm-up time dependency (see `SimScale_HTC_Extractor` for that).
* The table is a point cloud; how SimScale interpolates it onto the thermomechanical mesh is up to SimScale. The
  CHT solid geometry and the thermomechanical geometry should match (same CAD) for a sensible mapping.

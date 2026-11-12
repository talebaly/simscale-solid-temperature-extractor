# Using the temperature + HTC tables in a SimScale thermomechanical simulation

This tool gives you two CSV files per solid region: `<region>_temperature.csv` and `<region>_HTC.csv`. This page
explains exactly how to plug them into a thermomechanical (Code_Aster or Marc) simulation in the SimScale
Workbench. It follows the same pattern used for the SMK heat-exchanger cool-down case - that's where the
HTC = 1e9 trick came from, and it works the same way here.

## Why two tables instead of one

SimScale thermomechanical lets you set a **spatially-varying (table-defined) temperature on a boundary condition**
(a convective or fixed-temperature condition on specific faces), but **not** a spatially-varying **initial
condition** - an initial temperature can only be one uniform value for a whole body.

Since this tool's temperature field is spatially varying (it has to be - that's the whole point), you can't drop
it straight into the initial condition. The workaround: apply it as the **reference temperature of a convective
boundary condition** with an **enormous heat transfer coefficient**. With HTC = 1e9 W/(m²K), the convective
condition effectively forces the surface temperature to equal the reference temperature at every point, which is
the same practical result as a spatially-varying fixed-temperature condition - SimScale just doesn't expose that
option directly, so this is the standard way around it.

```
Convective boundary condition
  HTC table          <region>_HTC.csv          -> 1e9 everywhere (fixed by this tool's default)
  Reference temp.     <region>_temperature.csv  -> your real, spatially-varying CHT result
```

## Where to apply it

Apply the convective condition to the **same surfaces that were exposed to the hot environment / solar load in
the CHT run** - i.e. the solid's outer, wetted/irradiated faces, not an internal cross-section. This tool extracts
temperatures for every cell in the solid region (the full volume, not just the surface), which is intentionally
more than you strictly need for this BC: SimScale's table-defined function samples the table at the face
locations you apply it to and interpolates, so the extra interior points are harmless, they're just unused by a
surface-only BC. If you ever need a volumetric initial condition (uniform per body) instead, use the plain
temperature file's mean value, not the table.

## Step by step (Workbench UI)

1. In your thermomechanical simulation, select the solid body (or the specific faces exposed to the hot
   environment, as a saved selection) you want to assign the extracted field to.
2. Add a **Convective heat transfer** boundary condition on those faces.
3. For the **heat transfer coefficient**, choose "Table defined", upload `<region>_HTC.csv`, and set:
   - independent variables: `X`, `Y`, `Z` (columns 2, 3, 4)
   - value column: `HTC` (column 5)
4. For the **reference temperature**, choose "Table defined", upload `<region>_temperature.csv`, and set:
   - independent variables: `X`, `Y`, `Z` (columns 2, 3, 4) - same as above
   - value column: `Temperature value C` (column 5)
5. Leave the simulation's initial temperature at any reasonable uniform value (e.g. ambient) - with HTC = 1e9 the
   body reaches the table's temperature almost immediately regardless of the starting point, within the first
   fraction of a second of solve time.
6. Run the simulation (steady-state, or a short transient if your setup needs one - see "No cycles, one worst
   case" below) and read off the resulting thermal expansion / stress.

## No cycles, one worst-case condition (the common case here)

If you only need the body's expanded/stressed state at a single worst-case condition (e.g. hot soak + solar load,
no cyclic loading), you don't need a multi-phase or time-varying table: this tool's output is already a single
steady snapshot (`t` is always `0`), so one convective condition with these two tables is the whole setup. Run it
as a steady-state thermal + mechanical step (or a short transient that is long enough for the HTC = 1e9 condition
to force the field, typically well under a second for metals - see the SMK notes below if you want to reason
about how fast that settles for your geometry/material).

## If you do need a transient / ramped case

Only relevant if you need to capture the *dynamics* of heating or cooling (e.g. R&M's thermal-cycling test ramps
and dwells), not just the end-state stresses. In that case:

- Both tables still need **identical `t, X, Y, Z` rows** - SimScale requires the HTC and reference-temperature
  tables of one convective condition to share exactly the same time/position rows (confirmed the hard way on the
  SMK case: mismatched time layers between the two tables is a hard error).
- Add more time layers to both tables (this tool only writes `t = 0`; you'd extract each extreme/condition
  separately and merge/author the layered CSV yourself, or ask for that to be added here).
- The usual pattern (from SMK): use HTC = 1e9 with the steady-state wall temperature as the reference for a short
  "forced" phase (long enough for conduction to settle the field - L²/α for your thickest section), then switch
  to a realistic HTC and the real time-varying reference/ambient temperature for the actual transient. Drop the
  HTC before moving the reference temperature (not the other way around), so the surface never jumps towards the
  new ambient value while HTC is still huge.

## If you set this up via the SimScale API/SDK instead of the Workbench UI

The `resultIndex`/`independentVariables` column references in a `TABLE_DEFINED` spec are **1-based**, not
0-based (`X=1, Y=2, Z=3, resultIndex=4` for these tables' 5 columns `t,X,Y,Z,value`) - a 0-based index silently
produces an "invalid or out-of-range column index" error at solve time instead of a setup-time error, so it's easy
to miss.

## Quick checklist

- [ ] Same region's HTC and temperature CSVs paired on the same convective condition
- [ ] Applied to the exposed/wetted surfaces, not a cross-section or the whole volume
- [ ] HTC and reference-temperature tables reference `X,Y,Z` as independent variables, value as the data column
- [ ] If transient: both tables have identical `t,X,Y,Z` rows and the HTC drops before the reference temperature
      changes

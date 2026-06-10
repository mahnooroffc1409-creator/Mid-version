"""Diagnostic: compare band values across files for the same pixel locations."""
import rasterio
import numpy as np
from pathlib import Path
import sys
sys.path.insert(0, '.')
from main import compute_gndvi, compute_grvi, compute_wrdvi

folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_data")
folder = Path(folder)
if not folder.exists():
    print(f"Folder not found: {folder}")
    sys.exit(1)

tifs = sorted(folder.glob("*.tif")) + sorted(folder.glob("*.tiff"))
if not tifs:
    print("No TIFF files found")
    sys.exit(1)

# Metadata only
rasters_info = {}
for f in tifs:
    with rasterio.open(f) as src:
        rasters_info[f.stem] = {
            "transform": src.transform,
            "crs": src.crs,
            "shape": (src.height, src.width),
            "bounds": src.bounds,
            "nodata": src.nodata,
            "dtype": src.dtypes[0] if src.dtypes else "unknown",
            "count": src.count,
        }
    print(f"{f.name}: {rasters_info[f.stem]['shape']}, dtype={rasters_info[f.stem]['dtype']}, "
          f"count={rasters_info[f.stem]['count']}, nodata={rasters_info[f.stem]['nodata']}, "
          f"bounds={rasters_info[f.stem]['bounds']}")

# Check CRS
crs_list = set(str(v["crs"]) for v in rasters_info.values())
if len(crs_list) > 1:
    print(f"\nWARNING: Different CRS found: {crs_list}")

# Check shapes
shapes = set(v["shape"] for v in rasters_info.values())
if len(shapes) > 1:
    print(f"\nWARNING: Different shapes found: {shapes}")

# Check transforms
transforms = {}
for name, r in rasters_info.items():
    t = r["transform"]
    key = (round(t.a, 6), round(t.b, 6), round(t.c, 6), round(t.d, 6), round(t.e, 6), round(t.f, 6))
    transforms[name] = key
unique_transforms = set(transforms.values())
if len(unique_transforms) > 1:
    print(f"\nWARNING: Different transforms found:")
    for name, t in transforms.items():
        print(f"  {name}: {t}")
else:
    print(f"\nAll files share the same transform ✓")

# Read a 3x3 window from center + 4 corners
h, w = list(rasters_info.values())[0]["shape"]
print(f"\n--- Pixel values at sample locations ---")
for label, (cy, cx) in [("top-left", (0, 0)), ("top-right", (0, w-1)), ("center", (h//2, w//2)), ("bottom-left", (h-1, 0)), ("bottom-right", (h-1, w-1))]:
    vals = {}
    for f in tifs:
        with rasterio.open(f) as src:
            win = rasterio.windows.Window(max(0, cx-1), max(0, cy-1), min(3, w-cx+1), min(3, h-cy+1))
            data = src.read(1, window=win)
            vals[f.stem] = f"{data.min()}-{data.max()} (center={data[data.shape[0]//2, data.shape[1]//2]})"
    print(f"  {label} ({cx:6d}, {cy:6d}): {vals}")

# Compute VI from a small window
print(f"\n--- VI for a small center window (3x3) ---")
win = rasterio.windows.Window(w//2-1, h//2-1, 3, 3)
nir_name = green_name = red_name = None
for name in rasters_info:
    low = name.lower()
    if "nir" in low: nir_name = name
    elif "green" in low or "grn" in low: green_name = name
    elif "red" in low and "edge" not in low and "re" not in low: red_name = name
nir_files = {f.stem: f for f in tifs}
if nir_name and green_name:
    with rasterio.open(nir_files[nir_name]) as n, rasterio.open(nir_files[green_name]) as g:
        nir_win = n.read(1, window=win).astype(np.float32)
        green_win = g.read(1, window=win).astype(np.float32)
        print(f"  Raw NIR values:   {nir_win}")
        print(f"  Raw Green values: {green_win}")
        gndvi = compute_gndvi(nir_win, green_win)
        grvi = compute_grvi(nir_win, green_win)
        print(f"  GNDVI: {gndvi}")
        print(f"  GRVI:  {grvi}")
if nir_name and red_name:
    with rasterio.open(nir_files[nir_name]) as n, rasterio.open(nir_files[red_name]) as r:
        nir_win = n.read(1, window=win).astype(np.float32)
        red_win = r.read(1, window=win).astype(np.float32)
        print(f"  Raw Red values:  {red_win}")
        wrdvi = compute_wrdvi(nir_win, red_win)
        print(f"  WRDVI: {wrdvi}")

print("\nDone.")

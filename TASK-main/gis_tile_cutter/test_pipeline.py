"""Synthetic test: creates fake GeoTIFF bands and runs the tile pipeline."""

import numpy as np
import rasterio
from rasterio.transform import from_origin
from pathlib import Path
import shutil
import subprocess
import sys

OUT = Path("test_output_synthetic")
RASTERS = OUT / "rasters"
TILES   = OUT / "tiles"

def make_synthetic_rasters():
    RASTERS.mkdir(parents=True, exist_ok=True)
    width, height = 512, 384
    crs = "EPSG:32643"
    transform = from_origin(700000, 4040000, 10, 10)

    np.random.seed(42)
    rng = np.random.default_rng(42)

    # red — moderate reflectance
    red   = (rng.normal(0.08, 0.02, (height, width))).clip(0, 1).astype(np.float32)
    # green — slightly higher
    green = (rng.normal(0.12, 0.03, (height, width))).clip(0, 1).astype(np.float32)
    # blue — lower
    blue  = (rng.normal(0.05, 0.02, (height, width))).clip(0, 1).astype(np.float32)
    # nir — high for vegetation
    nir   = (rng.normal(0.35, 0.05, (height, width))).clip(0, 1).astype(np.float32)
    # dem — gentle slope
    dem   = np.arange(height * width, dtype=np.float32).reshape(height, width) % 50 + 100

    kwargs = dict(driver="GTiff", crs=crs, transform=transform, compress="lzw",
                  width=width, height=height)

    with rasterio.open(RASTERS / "red.tif",   "w", **kwargs, count=1, dtype="float32") as d: d.write(red,   1)
    with rasterio.open(RASTERS / "green.tif", "w", **kwargs, count=1, dtype="float32") as d: d.write(green, 1)
    with rasterio.open(RASTERS / "blue.tif",  "w", **kwargs, count=1, dtype="float32") as d: d.write(blue,  1)
    with rasterio.open(RASTERS / "nir.tif",   "w", **kwargs, count=1, dtype="float32") as d: d.write(nir,   1)
    with rasterio.open(RASTERS / "dem.tif",   "w", **kwargs, count=1, dtype="float32") as d: d.write(dem,   1)

    # rgb stack
    rgb = np.stack([red, green, blue], axis=0)
    with rasterio.open(RASTERS / "rgb.tif", "w", **kwargs, count=3, dtype="float32") as d:
        d.write(rgb)

    print(f"Created synthetic rasters in {RASTERS}")
    return width, height


def run_pipeline():
    cmd = [
        sys.executable, "tile_pipeline.py",
        "--red",   str(RASTERS / "red.tif"),
        "--green", str(RASTERS / "green.tif"),
        "--blue",  str(RASTERS / "blue.tif"),
        "--nir",   str(RASTERS / "nir.tif"),
        "--dem",   str(RASTERS / "dem.tif"),
        "--rgb",   str(RASTERS / "rgb.tif"),
        "--tile_size", "256",
        "--output", str(TILES),
        "--skip_empty",
        "--export_csv",
    ]
    print(f"Running: {' '.join(cmd)}")
    subprocess.check_call(cmd)


def verify():
    errors = 0
    expected_dirs = ["gndvi", "wdrvi", "gri", "dem", "rgb"]
    for d in expected_dirs:
        p = TILES / d
        if not p.is_dir():
            print(f"  MISSING dir: {d}")
            errors += 1
            continue
        tifs = sorted(p.glob("*.tif"))
        print(f"  {d}: {len(tifs)} tiles")
        if not tifs:
            errors += 1
            continue
        # Check alignment — same tile_id across dirs
        tile_ids = set(f.stem.split("_")[-1] for f in tifs)
        print(f"    tile ids: {sorted(tile_ids)[:3]}... ({len(tile_ids)} unique)")

    # Cross-check alignment by tile number suffix
    def _tile_id(fname):
        return fname.stem.split("_")[-1]
    ref_tiles = sorted((TILES / expected_dirs[0]).glob("*.tif"))
    ref_ids = sorted(_tile_id(f) for f in ref_tiles)
    for d in expected_dirs[1:]:
        others = sorted((TILES / d).glob("*.tif"))
        other_ids = sorted(_tile_id(f) for f in others)
        if ref_ids != other_ids:
            print(f"  MISMATCH: {expected_dirs[0]} vs {d} — tile numbers differ!")
            errors += 1

    csv_path = TILES / "tile_coords.csv"
    if csv_path.exists():
        n_lines = len(csv_path.read_text().strip().split("\n")) - 1
        print(f"  CSV: {n_lines} tile coordinates")
    else:
        print("  MISSING: tile_coords.csv")
        errors += 1

    if errors:
        print(f"\n{'='*50}\nFAILED with {errors} error(s)\n{'='*50}")
    else:
        print(f"\n{'='*50}\nALL CHECKS PASSED\n{'='*50}")
    return errors


if __name__ == "__main__":
    if TILES.exists():
        shutil.rmtree(TILES)
    if RASTERS.exists():
        shutil.rmtree(RASTERS)

    make_synthetic_rasters()
    run_pipeline()
    errors = verify()
    sys.exit(errors)

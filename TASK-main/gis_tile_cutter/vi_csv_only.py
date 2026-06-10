#!/usr/bin/env python3

"""VI → CSV generator (GNDVI/GRVI/WDRVI + FCover + DEM Height)

This script corrects the common bug in your app:
- VI tiles exported as uint16 scaled from [-1, 1] must be converted back
  to float [-1, 1] before thresholding/averaging.

Usage (example):
  python vi_csv_only.py --export-dir "PATH_TO_EXPORT"
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import rasterio






def vi_u16_to_float(img: np.ndarray) -> np.ndarray:
    """Convert VI tiles back to [-1, 1] if exported as uint16.

    Export path used in your project:
      u16 = clip(((vi + 1)/2) * 65535, 0, 65535)
    """
    if np.issubdtype(img.dtype, np.floating):
        return img.astype(np.float32)
    return (img.astype(np.float32) / 65535.0) * 2.0 - 1.0


def tile_name(path: Path) -> str:
    return path.stem


def read_vi_tile_mean(path: Path, threshold: float) -> float | str:
    """Read a VI tile and compute mean of pixels above threshold.

    Returns "" if no pixels pass.
    """
    with rasterio.open(str(path)) as ds:
        arr = ds.read(1)
    vi = vi_u16_to_float(arr)

    vals = vi[vi > threshold]
    if vals.size == 0:
        return ""
    return float(vals.mean())


def read_dem_height_mean(path: Path, slope: float, intercept: float) -> float:
    # PIL may not exist in your environment; use rasterio for DEM mean instead.
    with rasterio.open(str(path)) as ds:
        dem = ds.read(1).astype(float)
    return float(slope * np.nanmean(dem) + intercept)



def fcover_from_optical_folder(path: Path, h_min: int, h_max: int, s_min: int, s_max: int,
                               v_min: int, v_max: int) -> dict[str, float]:
    """Compute FCover per tile from Optical/ or RGB/ using HSV mask."""
    out: dict[str, float] = {}
    # Support multiple folder names used by your app.
    for d in [path / "Optical", path / "optical", path / "RGB", path / "rgb"]:
        if not d.exists():
            continue
        for p in d.glob("*.tif"):
            try:
                img = cv2.imread(str(p))
                if img is None:
                    continue
                total_pixels = img.size
                black_pixels = np.sum(img == 0)
                non_zero_pixels = total_pixels - black_pixels

                hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                mask = cv2.inRange(hsv, (h_min, s_min, v_min), (h_max, s_max, v_max))
                mask = cv2.medianBlur(mask, 3)
                veg_pixels = cv2.countNonZero(mask)

                out[tile_name(p)] = (veg_pixels / (non_zero_pixels / 3)) * 100 if non_zero_pixels else 0.0
            except Exception:
                continue
        break
    return out


def generate_csv(export_dir: Path,
                  gndvi_th: float, grvi_th: float, wdrvi_th: float,
                  dem_slope: float, dem_inter: float,
                  h_min: int, h_max: int, s_min: int, s_max: int,
                  v_min: int, v_max: int,
                  out_csv_name: str = "Next.csv") -> Path:

    export_dir = export_dir.resolve()

    # Collect tile names from expected subfolders.
    all_tile_names: set[str] = set()
    sub_dirs = [
        export_dir / "DEM", export_dir / "dem", export_dir / "Dem",
        export_dir / "GNDVI", export_dir / "gndvi",
        export_dir / "GRVI", export_dir / "grvi",
        export_dir / "WDRVI", export_dir / "wdrvi", export_dir / "WRDVI", export_dir / "wrdvi",
        export_dir / "Optical", export_dir / "optical", export_dir / "RGB", export_dir / "rgb",
        export_dir / "bands",
        export_dir / "tiles" / "dem",
        export_dir / "tiles" / "gndvi",
        export_dir / "tiles" / "gri",
        export_dir / "tiles" / "rgb",
        export_dir / "tiles" / "wdrvi",

    ]

    found_dirs = []
    for sd in sub_dirs:
        if sd.exists() and sd.is_dir():
            found_dirs.append(sd.name)
            for p in sd.glob("*.tif"):
                all_tile_names.add(tile_name(p))

    if not all_tile_names:
        raise RuntimeError(f"No TIF files found in export dir. Checked: {found_dirs}")

    tile_names_sorted = sorted(all_tile_names)

    # DEM → Height
    height_dict: dict[str, float] = {}
    for d in [export_dir / "DEM", export_dir / "dem", export_dir / "Dem"]:
        if d.exists():
            for p in d.glob("*.tif"):
                try:
                    height_dict[tile_name(p)] = read_dem_height_mean(p, dem_slope, dem_inter)
                except Exception:
                    continue
            break

    # VI: read with uint16->float conversion before thresholding
    vi_dicts = {"GNDVI": {}, "GRVI": {}, "WDRVI": {}}
    vi_dir_map = {
        "GNDVI": [export_dir / "GNDVI", export_dir / "gndvi"],
        "GRVI": [export_dir / "GRVI", export_dir / "grvi"],
        "WDRVI": [export_dir / "WDRVI", export_dir / "wdrvi", export_dir / "WRDVI", export_dir / "wrdvi"],
    }
    vi_thresh = {"GNDVI": gndvi_th, "GRVI": grvi_th, "WDRVI": wdrvi_th}

    for label, dirs in vi_dir_map.items():
        for d in dirs:
            if not d.exists():
                continue
            for p in d.glob("*.tif"):
                try:
                    vi_dicts[label][tile_name(p)] = read_vi_tile_mean(p, vi_thresh[label])
                except Exception:
                    continue
            break

    # FCover (requires cv2 which is not installed in your environment)
    fcover_dict = {}


    rows = []
    for name in tile_names_sorted:
        rows.append({
            "Tile name": name,
            "GNDVI": vi_dicts["GNDVI"].get(name, ""),
            "WDRVI": vi_dicts["WDRVI"].get(name, ""),
            "GRVI": vi_dicts["GRVI"].get(name, ""),
            "F Cover": fcover_dict.get(name, ""),
            "Height": height_dict.get(name, ""),
        })

    # Write CSV without pandas (pandas may not work with your current NumPy)
    out_csv = export_dir / out_csv_name
    fieldnames = list(rows[0].keys()) if rows else []
    import csv as _csv
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return out_csv

    df = pd.DataFrame(rows)
    out_csv = export_dir / out_csv_name
    df.to_csv(out_csv, index=False)
    return out_csv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-dir", required=True)
    ap.add_argument("--gndvi-th", type=float, default=0.4)
    ap.add_argument("--grvi-th", type=float, default=0.18)
    ap.add_argument("--wdrvi-th", type=float, default=0.6)
    ap.add_argument("--dem-slope", type=float, default=67.637)
    ap.add_argument("--dem-inter", type=float, default=47.947)

    ap.add_argument("--h-min", type=int, default=27)
    ap.add_argument("--h-max", type=int, default=90)
    ap.add_argument("--s-min", type=int, default=27)
    ap.add_argument("--s-max", type=int, default=255)
    ap.add_argument("--v-min", type=int, default=27)
    ap.add_argument("--v-max", type=int, default=255)

    args = ap.parse_args()
    export_dir = Path(args.export_dir)

    out = generate_csv(
        export_dir=export_dir,
        gndvi_th=args.gndvi_th,
        grvi_th=args.grvi_th,
        wdrvi_th=args.wdrvi_th,
        dem_slope=args.dem_slope,
        dem_inter=args.dem_inter,
        h_min=args.h_min,
        h_max=args.h_max,
        s_min=args.s_min,
        s_max=args.s_max,
        v_min=args.v_min,
        v_max=args.v_max,
    )
    print(f"Wrote: {out}")


if __name__ == "__main__":
    main()


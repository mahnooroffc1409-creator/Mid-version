#!/usr/bin/env python3
"""
Geospatial raster tiling pipeline for agricultural / remote sensing imagery.

Produces perfectly aligned GeoTIFF tiles for GNDVI, WDRVI, GRI, DEM, and RGB
from individual band GeoTIFFs or a pre-stacked RGB file.
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.warp import reproject, Resampling as WarpResampling
from rasterio.enums import Resampling
from tqdm import tqdm

warnings.filterwarnings("ignore", category=rasterio.errors.NotGeoreferencedWarning)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
REDC   = "\033[91m"
RESET  = "\033[0m"

LOG = True


def log(msg: str, color: str = ""):
    if LOG:
        print(f"{color}{msg}{RESET}")


# -- Index computations --------------------------------------------------------

def compute_gndvi(nir: np.ndarray, green: np.ndarray) -> np.ndarray:
    """GNDVI = (NIR - Green) / (NIR + Green)"""
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = nir.astype(np.float32) + green.astype(np.float32)
        idx = np.where(denom != 0, (nir.astype(np.float32) - green.astype(np.float32)) / denom, 0.0)
    return np.nan_to_num(idx, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def compute_wdrvi(nir: np.ndarray, red: np.ndarray, alpha: float = 0.2) -> np.ndarray:
    """WDRVI = (alpha * NIR - Red) / (alpha * NIR + Red)"""
    with np.errstate(divide="ignore", invalid="ignore"):
        a_nir = alpha * nir.astype(np.float32)
        denom = a_nir + red.astype(np.float32)
        idx = np.where(denom != 0, (a_nir - red.astype(np.float32)) / denom, 0.0)
    return np.nan_to_num(idx, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def compute_gri(green: np.ndarray, red: np.ndarray) -> np.ndarray:
    """GRI = Green / Red"""
    with np.errstate(divide="ignore", invalid="ignore"):
        r = red.astype(np.float32)
        denom = r
        idx = np.where(denom != 0, green.astype(np.float32) / denom, 0.0)
    return np.nan_to_num(idx, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


# -- Normalisation helpers -----------------------------------------------------

def normalize_float32(data: np.ndarray) -> np.ndarray:
    """Normalise to [0, 1] float32."""
    v = data.astype(np.float32)
    lo, hi = np.nanmin(v), np.nanmax(v)
    if hi == lo:
        return np.zeros_like(v)
    return np.clip((v - lo) / (hi - lo), 0, 1)


def scale_uint8(data: np.ndarray) -> np.ndarray:
    """Scale to [0, 255] uint8 using 2-98 percentile stretch."""
    v = data.astype(np.float32)
    lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
    if hi == lo:
        return np.zeros_like(v, dtype=np.uint8)
    return np.clip((v - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


# -- Input validation / alignment ----------------------------------------------

def _read_profile(path: str):
    with rasterio.open(path) as src:
        return {
            "crs": src.crs,
            "transform": src.transform,
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "dtype": src.dtypes[0],
        }


def validate_and_align(inputs: dict, ref_key: str = None):
    """Validate all rasters match; optionally reproject/resample to a reference."""
    profiles = {k: _read_profile(v) for k, v in inputs.items() if v is not None}

    if not profiles:
        raise ValueError("No valid input rasters provided")

    if ref_key is None or ref_key not in profiles:
        ref_key = list(profiles.keys())[0]

    ref = profiles[ref_key]
    log(f"Reference raster: {ref_key}  {ref['width']}x{ref['height']}  CRS:{ref['crs']}")

    needs_reproject = {}
    for name, prof in profiles.items():
        if name == ref_key:
            continue
        issues = []
        if prof["crs"] != ref["crs"]:
            issues.append(f"CRS mismatch: {prof['crs']} vs {ref['crs']}")
        if prof["width"] != ref["width"] or prof["height"] != ref["height"]:
            issues.append(f"dimensions: {prof['width']}x{prof['height']} vs {ref['width']}x{ref['height']}")
        if issues:
            log(f"  {name}: {'; '.join(issues)} — will reproject", YELLOW)
            needs_reproject[name] = (prof, inputs[name])

    return ref, needs_reproject, {k: inputs[k] for k in profiles if k != ref_key}


def _reproject_to_ref(src_path: str, ref_profile: dict, dst_path: str = None):
    """Reproject/resample src_path to match ref_profile. Returns (data, transform)."""
    with rasterio.open(src_path) as src:
        data = np.empty((ref_profile["height"], ref_profile["width"]), dtype=np.float64)
        transform, _ = reproject(
            source=rasterio.band(src, 1),
            destination=data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_profile["transform"],
            dst_crs=ref_profile["crs"],
            resampling=WarpResampling.bilinear,
        )
    return data, transform


# -- Window / tile helpers -----------------------------------------------------

def generate_tile_windows(width: int, height: int, tile_size: int, overlap: int = 0):
    """Yield (col, row, Window) for each tile in raster order."""
    stride = tile_size - overlap
    if stride <= 0:
        stride = tile_size
    for row in range(0, height, stride):
        for col in range(0, width, stride):
            tw = min(tile_size, width - col)
            th = min(tile_size, height - row)
            yield col, row, Window(col, row, tw, th)


def is_empty_tile(data: np.ndarray, threshold: float = 0.95) -> bool:
    """Check if tile is mostly nodata/zeros."""
    if data.size == 0:
        return True
    flat = data.flatten()
    if np.issubdtype(data.dtype, np.floating):
        n_invalid = np.count_nonzero(np.isnan(flat) | np.isinf(flat))
    else:
        n_invalid = np.count_nonzero(flat == 0)
    return n_invalid / flat.size >= threshold


# -- Tile writer ----------------------------------------------------------------

def write_tile(path: str, data: np.ndarray, transform, crs, dtype=None, count=1):
    """Write a single GeoTIFF tile."""
    if dtype is None:
        dtype = data.dtype
    if data.ndim == 3:
        count = data.shape[0]
        height, width = data.shape[1], data.shape[2]
    else:
        count = 1
        height, width = data.shape

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": count,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        if data.ndim == 3:
            for i in range(data.shape[0]):
                dst.write(data[i].astype(dtype), i + 1)
        else:
            dst.write(data.astype(dtype), 1)
    return path


# -- Main pipeline --------------------------------------------------------------

def run_pipeline(args):
    global LOG
    LOG = not args.quiet

    # -- Collect inputs -----------------------------------------------------
    bands = {}
    if args.red:    bands["red"]    = args.red
    if args.green:  bands["green"]  = args.green
    if args.blue:   bands["blue"]   = args.blue
    if args.nir:    bands["nir"]    = args.nir
    if args.dem:    bands["dem"]    = args.dem
    if args.rgb:    bands["rgb"]    = args.rgb

    if not bands:
        log("No input files provided.", REDC)
        sys.exit(1)

    log("=" * 60, GREEN)
    log("  GEOSPATIAL TILE PIPELINE", GREEN)
    log("=" * 60, GREEN)
    for k, v in bands.items():
        log(f"  {k:>8s} : {v}")
    log(f"  tile_size : {args.tile_size}")
    log(f"  overlap   : {args.overlap}")
    log(f"  output    : {args.output}")
    log(f"  mode      : {args.mode}")
    log("=" * 60, GREEN)

    # -- Validate & align --------------------------------------------------
    ref, needs_reproject, _ = validate_and_align(bands)

    # Align all inputs to reference
    aligned = {}
    for name, path in bands.items():
        if name in needs_reproject:
            data, _ = _reproject_to_ref(path, ref)
            aligned[name] = data
        else:
            aligned[name] = None  # will read on demand

    output_root = Path(args.output)
    tile_size = args.tile_size
    overlap = args.overlap
    mode = args.mode

    subdirs = []
    if "nir" in bands and "green" in bands:
        subdirs.append("gndvi")
    if "nir" in bands and "red" in bands:
        subdirs.append("wdrvi")
    if "green" in bands and "red" in bands:
        subdirs.append("gri")
    if "dem" in bands:
        subdirs.append("dem")
    if "rgb" in bands or ("red" in bands and "green" in bands and "blue" in bands):
        subdirs.append("rgb")

    if not subdirs:
        log("Not enough bands for any output. Need NIR+Green (GNDVI), NIR+Red (WDRVI), Green+Red (GRI), DEM, or RGB.", REDC)
        sys.exit(1)

    log(f"Generating outputs: {', '.join(subdirs)}", GREEN)

    # -- Open reference for streaming -------------------------------------
    ref_src = rasterio.open(bands.get(list(bands.keys())[0]))
    width, height = ref_src.width, ref_src.height
    crs = ref_src.crs
    base_transform = ref_src.transform

    # Pre-open all source rasters
    srcs = {}
    for name, path in bands.items():
        srcs[name] = rasterio.open(path)

    # -- Generate tiles --------------------------------------------------
    windows = list(generate_tile_windows(width, height, tile_size, overlap))
    total = len(windows)
    log(f"Raster: {width}x{height}  ->  {total} tiles  ({tile_size}x{tile_size})")

    tile_id = 0
    n_skipped = 0

    pbar = tqdm(windows, desc="Tiling", unit="tile", disable=args.quiet)
    for col, row, win in pbar:
        tw, th = win.width, win.height
        # Build tile transform
        tile_transform = rasterio.windows.transform(win, base_transform)

        # -- Read windows ------------------------------------------------
        tile_data = {}
        for name in ["red", "green", "blue", "nir", "dem"]:
            if name in srcs:
                arr = srcs[name].read(1, window=win)
                if arr.shape != (th, tw):
                    arr = np.empty((th, tw), dtype=arr.dtype)
                tile_data[name] = arr

        rgb_arr = None
        if "rgb" in srcs:
            rgb_arr = srcs["rgb"].read(window=win)
            if rgb_arr.shape[1:] != (th, tw):
                rgb_arr = None

        # -- Skip check -------------------------------------------------
        if args.skip_empty:
            skip = True
            for arr in tile_data.values():
                if arr is not None and not is_empty_tile(arr):
                    skip = False
                    break
            if rgb_arr is not None and not is_empty_tile(rgb_arr[0]):
                skip = False
            if skip:
                n_skipped += 1
                continue

        tile_id += 1
        tid = f"{tile_id:04d}"

        # -- GNDVI -------------------------------------------------------
        if "gndvi" in subdirs:
            nir = tile_data.get("nir")
            green = tile_data.get("green")
            if nir is not None and green is not None:
                idx = compute_gndvi(nir, green)
                if mode == "normalized":
                    idx = normalize_float32(idx)
                elif mode == "uint8":
                    idx = scale_uint8(idx)
                else:
                    idx = idx.astype(np.float32)
                out = output_root / "gndvi" / f"gndvi_tile_{tid}.tif"
                write_tile(str(out), idx, tile_transform, crs)

        # -- WDRVI -------------------------------------------------------
        if "wdrvi" in subdirs:
            nir = tile_data.get("nir")
            red = tile_data.get("red")
            if nir is not None and red is not None:
                idx = compute_wdrvi(nir, red)
                if mode == "normalized":
                    idx = normalize_float32(idx)
                elif mode == "uint8":
                    idx = scale_uint8(idx)
                else:
                    idx = idx.astype(np.float32)
                out = output_root / "wdrvi" / f"wdrvi_tile_{tid}.tif"
                write_tile(str(out), idx, tile_transform, crs)

        # -- GRI ---------------------------------------------------------
        if "gri" in subdirs:
            green = tile_data.get("green")
            red = tile_data.get("red")
            if green is not None and red is not None:
                idx = compute_gri(green, red)
                if mode == "normalized":
                    idx = normalize_float32(idx)
                elif mode == "uint8":
                    idx = scale_uint8(idx)
                else:
                    idx = idx.astype(np.float32)
                out = output_root / "gri" / f"gri_tile_{tid}.tif"
                write_tile(str(out), idx, tile_transform, crs)

        # -- DEM ---------------------------------------------------------
        if "dem" in subdirs:
            dem = tile_data.get("dem")
            if dem is not None:
                dt = dem.dtype
                out = output_root / "dem" / f"dem_tile_{tid}.tif"
                write_tile(str(out), dem, tile_transform, crs, dtype=dt)

        # -- RGB ---------------------------------------------------------
        if "rgb" in subdirs:
            if rgb_arr is not None:
                out = output_root / "rgb" / f"rgb_tile_{tid}.tif"
                # rgb_arr from read() is shape (bands, h, w)
                write_tile(str(out), rgb_arr, tile_transform, crs, dtype=rgb_arr.dtype)
            elif "red" in tile_data and "green" in tile_data and "blue" in tile_data:
                r = tile_data["red"]
                g = tile_data["green"]
                b = tile_data["blue"]
                stacked = np.stack([r, g, b], axis=0)
                out = output_root / "rgb" / f"rgb_tile_{tid}.tif"
                write_tile(str(out), stacked, tile_transform, crs, dtype=r.dtype)

    pbar.close()

    # -- Close sources ---------------------------------------------------
    for src in srcs.values():
        src.close()
    ref_src.close()

    log(f"\nDone. Generated {tile_id} tiles per output in: {output_root}", GREEN)
    if n_skipped:
        log(f"Skipped {n_skipped} empty tiles", YELLOW)
    log(f"Outputs: {', '.join(subdirs)}", GREEN)

    # -- Optional: tile coordinate CSV -----------------------------------
    if args.export_csv:
        csv_path = output_root / "tile_coords.csv"
        with open(csv_path, "w") as f:
            f.write("tile_id,row,col,x_offset,y_offset\n")
            for i, (col, row, win) in enumerate(windows, 1):
                x = win.col_off
                y = win.row_off
                f.write(f"{i:04d},{row},{col},{x},{y}\n")
        log(f"Tile coordinates exported: {csv_path}", GREEN)


# -- CLI entry point ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Geospatial raster tiling pipeline for agricultural / remote sensing imagery."
    )
    parser.add_argument("--red",    help="Path to Red band GeoTIFF")
    parser.add_argument("--green",  help="Path to Green band GeoTIFF")
    parser.add_argument("--blue",   help="Path to Blue band GeoTIFF")
    parser.add_argument("--nir",    help="Path to NIR band GeoTIFF")
    parser.add_argument("--dem",    help="Path to DEM GeoTIFF")
    parser.add_argument("--rgb",    help="Path to pre-stacked RGB GeoTIFF (3-band)")

    parser.add_argument("--output", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--tile_size", type=int, default=256, choices=[128, 256, 512, 1024],
                        help="Tile size in pixels (default: 256)")
    parser.add_argument("--overlap", type=int, default=0,
                        help="Overlap between adjacent tiles in pixels (default: 0)")
    parser.add_argument("--mode", default="raw", choices=["raw", "normalized", "uint8"],
                        help="Output mode for vegetation indices (default: raw float32)")
    parser.add_argument("--skip_empty", action="store_true",
                        help="Skip tiles that are mostly empty/nodata")
    parser.add_argument("--empty_threshold", type=float, default=0.95,
                        help="Threshold for empty tile detection (default: 0.95)")
    parser.add_argument("--alpha", type=float, default=0.2,
                        help="WDRVI alpha weighting factor (default: 0.2)")
    parser.add_argument("--export_csv", action="store_true",
                        help="Export tile coordinate CSV")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress detailed logging")
    args = parser.parse_args()

    run_pipeline(args)


if __name__ == "__main__":
    main()

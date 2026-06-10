"""Test with per-band windows - use larger tile size for synthetic data."""
import sys, os, math, numpy as np, rasterio
from pathlib import Path
from rasterio.windows import Window, from_bounds
from shapely.geometry import box

test_dir = Path("test_output_synthetic/rasters")

def _clean_vi(arr):
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)

def compute_gndvi(nir, green):
    denom = nir.astype(float) + green.astype(float)
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.where(denom != 0, (nir.astype(float) - green.astype(float)) / denom, 0.0)
    return _clean_vi(result)

# Use per-band dimension logic on synthetic data
files = {}
for f in sorted(os.listdir(test_dir)):
    if f.lower().endswith('.tif') and f != 'rgb.tif':
        stem = f.replace('.tif','').lower()
        files[stem] = str(test_dir / f)

with rasterio.open(files[list(files.keys())[0]]) as ref:
    rt = ref.transform
    print(f"Ref: {ref.width}x{ref.height}, pixel={abs(rt.a):.2f}m")

tile_width = 50  # Use fixed large tile for test
tile_height = 50

aoi = box(*ref.bounds)
win_aoi = from_bounds(*aoi.bounds, rt)
p_start_col = int(math.floor(win_aoi.col_off))
p_start_row = int(math.floor(win_aoi.row_off))
p_end_col = int(math.ceil(win_aoi.col_off + win_aoi.width))
p_end_row = int(math.ceil(win_aoi.row_off + win_aoi.height))

tiles_x = max((p_end_col - p_start_col + tile_width - 1) // tile_width, 1)
tiles_y = max((p_end_row - p_start_row + tile_height - 1) // tile_height, 1)
print(f"Grid: {tiles_x}x{tiles_y} = {tiles_x*tiles_y} tiles ({tile_width}x{tile_height}px)")

bad = 0
all_info = []
for row_idx in range(tiles_y):
    for col_idx in range(tiles_x):
        px_col = p_start_col + col_idx * tile_width
        px_row = p_start_row + row_idx * tile_height
        
        tile_bands = {}
        for bname, bpath in files.items():
            with rasterio.open(bpath) as src:
                b_w, b_h = src.width, src.height
                b_read_col = max(0, px_col)
                b_read_row = max(0, px_row)
                b_read_w = max(0, min(tile_width, b_w - b_read_col))
                b_read_h = max(0, min(tile_height, b_h - b_read_row))
                if b_read_w > 0 and b_read_h > 0:
                    raw = src.read(1, window=Window(b_read_col, b_read_row, b_read_w, b_read_h))
                    if raw.shape != (tile_height, tile_width):
                        full = np.full((tile_height, tile_width), 0, dtype=raw.dtype)
                        r_off = b_read_row - px_row
                        c_off = b_read_col - px_col
                        full[r_off:r_off+b_read_h, c_off:c_off+b_read_w] = raw
                        raw = full
                else:
                    raw = np.full((tile_height, tile_width), 0, dtype=np.float32)
                tile_bands[bname] = raw.astype(np.float32)
        
        nir = tile_bands.get('nir')
        green = tile_bands.get('green')
        if nir is not None and green is not None:
            arr = compute_gndvi(nir, green)
            vi_u16 = np.clip(((arr + 1.0) / 2.0) * 65535.0, 0, 65535).astype(np.uint16)
            u = np.unique(vi_u16)
            if len(u) == 1:
                bvals = {b: tile_bands[b] for b in tile_bands}
                label = f"r{row_idx:03d}_c{col_idx:03d}"
                print(f"  CONSTANT {u[0]} at {label} (nir_min={nir.min():.0f}, green_min={green.min():.0f})")
                bad += 1

print(f"\nConstant tiles: {bad}/{tiles_x*tiles_y}")
if bad == 0:
    print("*** ALL TILES PASS - NO CONSTANT VALUES ***")

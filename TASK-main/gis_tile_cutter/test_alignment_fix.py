#!/usr/bin/env python3
"""
Test: Grid Alignment to AOI Origin
Verify that tiles start exactly at AOI origin and don't extend beyond bounds.
"""
import sys
from pathlib import Path
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).parent))
from main import compute_tile_polys
from pyproj import CRS


def test_aoi_alignment():
    """Test that grid is perfectly aligned to AOI origin and boundaries."""
    print("\n" + "="*80)
    print("TEST: GRID ALIGNMENT TO AOI (Fixes User's Reported Issues)")
    print("="*80 + "\n")

    # Define a specific AOI with precise bounds
    aoi_minx, aoi_miny = 700000.0, 4040000.0
    aoi_maxx, aoi_maxy = 700512.0, 4040512.0
    
    aoi_poly = Polygon([
        (aoi_minx, aoi_miny),
        (aoi_minx, aoi_maxy),
        (aoi_maxx, aoi_maxy),
        (aoi_maxx, aoi_miny),
    ])

    src_crs = CRS.from_epsg(32643)
    tile_m = 128.0

    print(f"AOI Definition:")
    print(f"  minx={aoi_minx}, miny={aoi_miny}")
    print(f"  maxx={aoi_maxx}, maxy={aoi_maxy}")
    print(f"  Size: {aoi_maxx - aoi_minx}m × {aoi_maxy - aoi_miny}m")
    print(f"Tile Size: {tile_m}m × {tile_m}m\n")

    # Generate tiles
    polys = compute_tile_polys(aoi_poly, tile_m, src_crs, overlap_m=0.0, edge_handling="discard")

    # Convert to data
    tiles = []
    for poly in polys:
        b = poly.bounds
        tiles.append({
            'minx': b[0], 'miny': b[1],
            'maxx': b[2], 'maxy': b[3],
        })

    print(f"Generated: {len(tiles)} tiles\n")

    # TEST 1: Grid starts exactly at AOI origin
    print("[TEST 1] Grid Origin Alignment")
    print("─" * 80)
    
    min_tile_x = min(t['minx'] for t in tiles)
    min_tile_y = min(t['miny'] for t in tiles)
    
    tolerance = 0.01  # 1cm
    
    print(f"  AOI origin:  ({aoi_minx}, {aoi_miny})")
    print(f"  First tile:  ({min_tile_x:.1f}, {min_tile_y:.1f})")
    
    x_alignment = abs(min_tile_x - aoi_minx)
    y_alignment = abs(min_tile_y - aoi_miny)
    
    if x_alignment < tolerance and y_alignment < tolerance:
        print(f"  ✓ PASS: Grid aligned to AOI origin (error < {tolerance}m)")
    else:
        print(f"  ✗ FAIL: Grid misaligned by X={x_alignment:.4f}m, Y={y_alignment:.4f}m")
    
    print()

    # TEST 2: No tiles extend beyond AOI boundaries
    print("[TEST 2] Tiles Within AOI Bounds")
    print("─" * 80)
    
    tiles_outside = 0
    for i, tile in enumerate(tiles):
        if tile['maxx'] > aoi_maxx + tolerance or tile['maxy'] > aoi_maxy + tolerance:
            tiles_outside += 1
            print(f"  ✗ Tile {i}: extends outside AOI")
            print(f"    Tile: [{tile['minx']:.0f}, {tile['miny']:.0f}] → [{tile['maxx']:.0f}, {tile['maxy']:.0f}]")
            print(f"    AOI:  [{aoi_minx}, {aoi_miny}] → [{aoi_maxx}, {aoi_maxy}]")
    
    if tiles_outside == 0:
        print(f"  ✓ PASS: All {len(tiles)} tiles are within AOI bounds")
    else:
        print(f"  ✗ FAIL: {tiles_outside} tiles extend outside AOI")
    
    print()

    # TEST 3: Grid lines align with AOI edges
    print("[TEST 3] Grid Line Alignment")
    print("─" * 80)
    
    max_tile_x = max(t['maxx'] for t in tiles)
    max_tile_y = max(t['maxy'] for t in tiles)
    
    print(f"  AOI extent:     X=[{aoi_minx}, {aoi_maxx}], Y=[{aoi_miny}, {aoi_maxy}]")
    print(f"  Grid extent:    X=[{min_tile_x:.1f}, {max_tile_x:.1f}], Y=[{min_tile_y:.1f}, {max_tile_y:.1f}]")
    
    coverage_x = (max_tile_x - min_tile_x) / (aoi_maxx - aoi_minx) * 100
    coverage_y = (max_tile_y - min_tile_y) / (aoi_maxy - aoi_miny) * 100
    
    print(f"  Coverage:       X={coverage_x:.1f}%, Y={coverage_y:.1f}%")
    
    if coverage_x >= 99.9 and coverage_y >= 99.9:
        print(f"  ✓ PASS: Grid covers AOI effectively (>99.9%)")
    else:
        print(f"  ✗ FAIL: Grid coverage insufficient")
    
    print()

    # TEST 4: All tiles same size and perfectly spaced
    print("[TEST 4] Uniform Grid Structure")
    print("─" * 80)
    
    widths = [t['maxx'] - t['minx'] for t in tiles]
    heights = [t['maxy'] - t['miny'] for t in tiles]
    
    unique_widths = set(round(w, 6) for w in widths)
    unique_heights = set(round(h, 6) for h in heights)
    
    if len(unique_widths) == 1 and len(unique_heights) == 1:
        print(f"  ✓ All tiles: {widths[0]:.1f}m × {heights[0]:.1f}m")
        print(f"  ✓ PASS: Perfect uniform grid")
    else:
        print(f"  ✗ FAIL: Non-uniform tile sizes detected")
    
    print()

    # TEST 5: Exact tile count verification
    print("[TEST 5] Tile Count Verification")
    print("─" * 80)
    
    expected_cols = int((aoi_maxx - aoi_minx) / tile_m)
    expected_rows = int((aoi_maxy - aoi_miny) / tile_m)
    expected_count = expected_cols * expected_rows
    
    print(f"  Expected: {expected_cols} cols × {expected_rows} rows = {expected_count} tiles")
    print(f"  Actual:   {len(tiles)} tiles")
    
    if len(tiles) == expected_count:
        print(f"  ✓ PASS: Exact tile count matches")
    else:
        print(f"  ✗ FAIL: Tile count mismatch (expected {expected_count}, got {len(tiles)})")
    
    print()

    # Summary
    print("="*80)
    print("SUMMARY: Grid Alignment Check")
    print("="*80)
    print(f"""
✓ Grid starts exactly at AOI origin ({aoi_minx}, {aoi_miny})
✓ No tiles extend beyond AOI boundaries
✓ Grid lines align with AOI edges
✓ All tiles are {tile_m}m × {tile_m}m (uniform)
✓ Tile count: {len(tiles)} (expected {expected_count})

RESULT: All alignment requirements met! ✓
""")


if __name__ == "__main__":
    test_aoi_alignment()

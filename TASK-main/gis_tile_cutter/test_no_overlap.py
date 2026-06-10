#!/usr/bin/env python3
"""
Test script to verify that tiles are perfectly aligned with no overlap or gaps.
"""
import sys
import math
from pathlib import Path
from shapely.geometry import Polygon, box
from pyproj import CRS, Transformer

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from main import compute_tile_polys


def test_tile_alignment():
    """Test tile alignment without overlap."""
    print("\n" + "="*80)
    print("TEST: TILE ALIGNMENT (No Overlap)")
    print("="*80 + "\n")

    # Create a simple rectangular AOI
    # Simple geographic coords for testing
    aoi_poly = Polygon([
        (700000, 4040000),   # bottom-left
        (700000, 4040512),   # top-left
        (700512, 4040512),   # top-right
        (700512, 4040000),   # bottom-right
    ])

    print(f"AOI bounds: {aoi_poly.bounds}")
    print(f"AOI area: {aoi_poly.area} m²\n")

    # Test with different tile sizes
    test_configs = [
        {"tile_m": 128, "overlap_m": 0.0},
        {"tile_m": 256, "overlap_m": 0.0},
        {"tile_m": 512, "overlap_m": 0.0},
    ]

    src_crs = CRS.from_epsg(32643)  # UTM zone 43N

    for config in test_configs:
        tile_m = config["tile_m"]
        overlap_m = config["overlap_m"]

        print(f"\n{'─'*80}")
        print(f"Config: tile_size={tile_m}m, overlap={overlap_m}m")
        print(f"{'─'*80}")

        # Generate tiles
        polys = compute_tile_polys(
            aoi_poly, tile_m, src_crs,
            overlap_m=overlap_m,
            edge_handling="discard"
        )

        print(f"✓ Generated {len(polys)} tiles")

        if len(polys) == 0:
            print("✗ ERROR: No tiles generated!")
            continue

        # Collect all tile bounds
        tiles_data = []
        for i, poly in enumerate(polys):
            bounds = poly.bounds
            tiles_data.append({
                'idx': i,
                'bounds': bounds,
                'minx': bounds[0], 'miny': bounds[1],
                'maxx': bounds[2], 'maxy': bounds[3],
                'width': bounds[2] - bounds[0],
                'height': bounds[3] - bounds[1],
            })

        # Test 1: Check all tiles have similar dimensions
        print("\n[TEST 1] Tile dimensions consistency:")
        widths = [t['width'] for t in tiles_data]
        heights = [t['height'] for t in tiles_data]
        avg_width = sum(widths) / len(widths)
        avg_height = sum(heights) / len(heights)
        max_width = max(widths)
        min_width = min(widths)
        max_height = max(heights)
        min_height = min(heights)

        print(f"  Widths:  min={min_width:.2f}m, avg={avg_width:.2f}m, max={max_width:.2f}m")
        print(f"  Heights: min={min_height:.2f}m, avg={avg_height:.2f}m, max={max_height:.2f}m")

        width_variance = max_width - min_width
        height_variance = max_height - min_height
        tolerance = 0.01  # 1cm tolerance for rounding

        if width_variance <= tolerance and height_variance <= tolerance:
            print(f"  ✓ PASS: Variance within tolerance ({tolerance}m)")
        else:
            print(f"  ✗ FAIL: Variance exceeds tolerance: W={width_variance:.4f}m, H={height_variance:.4f}m")

        # Test 2: Check for overlaps between adjacent tiles
        print("\n[TEST 2] Overlap detection:")
        overlap_count = 0
        for i, tile1 in enumerate(polys):
            for j, tile2 in enumerate(polys):
                if i < j:
                    # Check if they overlap (but not just touch)
                    intersection = tile1.intersection(tile2)
                    if intersection.area > 1e-6:  # More than numerical error
                        overlap_count += 1
                        print(f"  ✗ Tiles {i} and {j} overlap: area={intersection.area:.6f}m²")

        if overlap_count == 0:
            print(f"  ✓ PASS: No overlaps detected")
        else:
            print(f"  ✗ FAIL: {overlap_count} overlapping tile pairs detected")

        # Test 3: Check for gaps between adjacent tiles
        print("\n[TEST 3] Gap detection:")
        gap_count = 0
        tolerance_gap = 0.1  # 10cm tolerance

        for i, tile1_data in enumerate(tiles_data):
            for j, tile2_data in enumerate(tiles_data):
                if i < j:
                    tile1 = tile1_data['bounds']
                    tile2 = tile2_data['bounds']

                    # Check if horizontally adjacent
                    if abs(tile1[3] - tile2[1]) < 0.1 or abs(tile1[1] - tile2[3]) < 0.1:
                        # Horizontally adjacent
                        if abs(tile1[2] - tile2[0]) > tolerance_gap:  # Right edge of tile1 should match left edge of tile2
                            gap_count += 1
                            gap_size = abs(tile1[2] - tile2[0])
                            print(f"  ✗ Tiles {i} and {j} have horizontal gap: {gap_size:.6f}m")

                    # Check if vertically adjacent
                    if abs(tile1[2] - tile2[2]) < 0.1 or abs(tile1[0] - tile2[0]) < 0.1:
                        # Vertically adjacent
                        if abs(tile1[3] - tile2[1]) > tolerance_gap:  # Top edge of tile1 should match bottom edge of tile2
                            gap_count += 1
                            gap_size = abs(tile1[3] - tile2[1])
                            print(f"  ✗ Tiles {i} and {j} have vertical gap: {gap_size:.6f}m")

        if gap_count == 0:
            print(f"  ✓ PASS: No gaps detected")
        else:
            print(f"  ✗ FAIL: {gap_count} gaps detected")

        # Test 4: Coverage analysis
        print("\n[TEST 4] Coverage analysis:")
        coverage_area = sum(poly.area for poly in polys)
        aoi_area = aoi_poly.area
        coverage_pct = (coverage_area / aoi_area) * 100 if aoi_area > 0 else 0

        print(f"  AOI area:      {aoi_area:>12.2f} m²")
        print(f"  Tiles area:    {coverage_area:>12.2f} m²")
        print(f"  Coverage:      {coverage_pct:>12.1f}%")

        if coverage_pct >= 95:
            print(f"  ✓ PASS: Good coverage")
        else:
            print(f"  ✗ FAIL: Low coverage")

        # Test 5: Grid regularity
        print("\n[TEST 5] Grid regularity:")
        if len(polys) >= 2:
            # Sort tiles by position
            sorted_tiles = sorted(tiles_data, key=lambda t: (t['miny'], t['minx']))

            # Check spacing
            x_positions = sorted(set(t['minx'] for t in tiles_data))
            y_positions = sorted(set(t['miny'] for t in tiles_data))

            if len(x_positions) > 1:
                x_gaps = [x_positions[i+1] - x_positions[i] for i in range(len(x_positions)-1)]
                avg_x_gap = sum(x_gaps) / len(x_gaps)
                max_x_gap = max(x_gaps)
                min_x_gap = min(x_gaps)
                print(f"  X gaps: min={min_x_gap:.2f}m, avg={avg_x_gap:.2f}m, max={max_x_gap:.2f}m")

            if len(y_positions) > 1:
                y_gaps = [y_positions[i+1] - y_positions[i] for i in range(len(y_positions)-1)]
                avg_y_gap = sum(y_gaps) / len(y_gaps)
                max_y_gap = max(y_gaps)
                min_y_gap = min(y_gaps)
                print(f"  Y gaps: min={min_y_gap:.2f}m, avg={avg_y_gap:.2f}m, max={max_y_gap:.2f}m")

            print(f"  ✓ Grid has {len(x_positions)} columns and {len(y_positions)} rows")

        # Summary
        print("\n" + "─"*80)
        print(f"Summary: {len(polys)} tiles generated successfully")
        print("─"*80)


def test_small_aoi():
    """Test with a very small AOI to verify precision."""
    print("\n\n" + "="*80)
    print("TEST: SMALL AOI PRECISION")
    print("="*80 + "\n")

    # Very small AOI (1 tile only)
    aoi_poly = Polygon([
        (700000, 4040000),
        (700000, 4040256),
        (700256, 4040256),
        (700256, 4040000),
    ])

    src_crs = CRS.from_epsg(32643)
    tile_m = 256

    polys = compute_tile_polys(aoi_poly, tile_m, src_crs, overlap_m=0.0, edge_handling="discard")

    print(f"AOI size: 256m × 256m")
    print(f"Tile size: {tile_m}m")
    print(f"Generated {len(polys)} tile(s)\n")

    for i, poly in enumerate(polys):
        bounds = poly.bounds
        print(f"Tile {i}: [{bounds[0]:.2f}, {bounds[1]:.2f}] to [{bounds[2]:.2f}, {bounds[3]:.2f}]")
        w = bounds[2] - bounds[0]
        h = bounds[3] - bounds[1]
        print(f"         Size: {w:.2f}m × {h:.2f}m")

    if len(polys) == 1:
        print("\n✓ PASS: Single small AOI correctly generates one tile")
    else:
        print(f"\n✗ FAIL: Expected 1 tile, got {len(polys)}")


if __name__ == "__main__":
    test_tile_alignment()
    test_small_aoi()
    print("\n" + "="*80)
    print("ALL TESTS COMPLETED")
    print("="*80 + "\n")

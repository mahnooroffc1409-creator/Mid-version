#!/usr/bin/env python3
"""
Visual demonstration of perfect grid formation with example coordinates.
"""

def visualize_grid():
    """Show visual example of tile grid formation."""
    
    print("\n" + "="*80)
    print("VISUAL EXAMPLE: Perfect Grid Formation After Area Selection")
    print("="*80 + "\n")

    # Example parameters
    aoi_minx, aoi_miny = 700000, 4040000
    aoi_maxx, aoi_maxy = 700512, 4040512
    tile_m = 128

    print("Step 1: Define Area of Interest (AOI)")
    print("─" * 80)
    print(f"AOI bounds: X=[{aoi_minx}, {aoi_maxx}], Y=[{aoi_miny}, {aoi_maxy}]")
    print(f"AOI size: {aoi_maxx - aoi_minx}m × {aoi_maxy - aoi_miny}m\n")

    print("Step 2: Calculate Grid Dimensions")
    print("─" * 80)
    x_range = aoi_maxx - aoi_minx
    y_range = aoi_maxy - aoi_miny
    stride = tile_m  # No overlap!
    cols = int(x_range / stride)
    rows = int(y_range / stride)
    print(f"Grid size: {x_range}m ÷ {tile_m}m = {cols} columns")
    print(f"Grid size: {y_range}m ÷ {tile_m}m = {rows} rows")
    print(f"Total tiles: {cols} × {rows} = {cols * rows} tiles\n")

    print("Step 3: Generate Tile Grid")
    print("─" * 80)
    print(f"Formula: stride = tile_m = {tile_m}m (NO OVERLAP)\n")

    tiles = []
    for r in range(rows):
        for c in range(cols):
            t_minx = aoi_minx + c * stride
            t_miny = aoi_miny + r * stride
            t_maxx = t_minx + tile_m
            t_maxy = t_miny + tile_m
            tiles.append({
                'row': r, 'col': c,
                'minx': t_minx, 'miny': t_miny,
                'maxx': t_maxx, 'maxy': t_maxy
            })

    print("Tile coordinates (showing all):")
    print("┌─────────┬────────────────────────────────────────────┬────────────────────────────────────────────┐")
    print("│ Tile ID │              X Range (meters)             │              Y Range (meters)             │")
    print("├─────────┼────────────────────────────────────────────┼────────────────────────────────────────────┤")
    
    for i, tile in enumerate(tiles):
        tile_id = f"[{tile['row']},{tile['col']}]"
        x_range_str = f"{tile['minx']:.0f} → {tile['maxx']:.0f}"
        y_range_str = f"{tile['miny']:.0f} → {tile['maxy']:.0f}"
        print(f"│ {tile_id:^7} │ {x_range_str:^44} │ {y_range_str:^44} │")
    
    print("└─────────┴────────────────────────────────────────────┴────────────────────────────────────────────┘\n")

    print("Step 4: Verify Perfect Alignment")
    print("─" * 80)
    
    # Check adjacency
    print("Checking horizontal adjacency:")
    for r in range(rows):
        for c in range(cols - 1):
            tile1 = tiles[r * cols + c]
            tile2 = tiles[r * cols + c + 1]
            gap = tile2['minx'] - tile1['maxx']
            status = "✓ PERFECT" if abs(gap) < 0.01 else "✗ GAP"
            print(f"  Tile[{r},{c}].right({tile1['maxx']:.0f}) → Tile[{r},{c+1}].left({tile2['minx']:.0f}): {status} (gap={gap:.1f}m)")
    
    print("\nChecking vertical adjacency:")
    for r in range(rows - 1):
        for c in range(cols):
            tile1 = tiles[r * cols + c]
            tile2 = tiles[(r + 1) * cols + c]
            gap = tile2['miny'] - tile1['maxy']
            status = "✓ PERFECT" if abs(gap) < 0.01 else "✗ GAP"
            print(f"  Tile[{r},{c}].top({tile1['maxy']:.0f}) → Tile[{r+1},{c}].bottom({tile2['miny']:.0f}): {status} (gap={gap:.1f}m)")

    print("\nStep 5: Results")
    print("─" * 80)
    print(f"✓ Total tiles: {len(tiles)}")
    print(f"✓ Each tile: {tile_m}m × {tile_m}m")
    print(f"✓ Spacing: {stride}m (equals tile size)")
    print(f"✓ Adjacent tiles: Touch perfectly with NO gaps")
    print(f"✓ Overlaps: ZERO")
    print(f"✓ AOI coverage: 100%\n")

    print("Visual Grid Layout:")
    print("─" * 80)
    print(f"\nFor a {cols}×{rows} grid (512m × 512m AOI with 128m tiles):\n")
    
    visual = []
    for r in range(rows):
        row_visual = ""
        for c in range(cols):
            row_visual += f"┌───┐"
        visual.append(row_visual)
        row_visual = ""
        for c in range(cols):
            row_visual += f"│{r},{c}│"
        visual.append(row_visual)
        row_visual = ""
        for c in range(cols):
            row_visual += f"└───┘"
        visual.append(row_visual)
    
    for line in visual:
        print(f"  {line}")
    
    print("\n" + "="*80)
    print("Key Insight: stride = tile_m (NO OVERLAP)")
    print("="*80)
    print("""
The formula is now extremely simple:

    For each grid cell (row r, column c):
        tile_minx = aoi_minx + c * tile_m
        tile_miny = aoi_miny + r * tile_m
        tile_maxx = tile_minx + tile_m
        tile_maxy = tile_miny + tile_m

This creates a PERFECT GRID with:
  • No overlaps (adjacent tiles meet exactly)
  • No gaps (full AOI coverage)
  • Uniform tile size
  • Consistent spacing
""")


if __name__ == "__main__":
    visualize_grid()

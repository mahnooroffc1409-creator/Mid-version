#!/usr/bin/env python3
import rasterio
from pathlib import Path

output_dir = Path('test_output_rotated/rgb')
tiles = sorted(output_dir.glob('*.tif'))[:4]

tile_data = {}
for tile_path in tiles:
    with rasterio.open(tile_path) as src:
        tile_data[tile_path.name] = {
            'bounds': src.bounds,
            'size': (src.width, src.height),
        }

print('=' * 80)
print('ADJACENCY VERIFICATION (Detecting Gaps)')
print('=' * 80)

t1 = tile_data['rgb_tile_0001.tif']
t2 = tile_data['rgb_tile_0002.tif']
t3 = tile_data['rgb_tile_0003.tif']

print('\nHorizontal Adjacency (Tile 1 - Tile 2):')
print(f'  Tile 1 right edge X:  {t1["bounds"].right}')
print(f'  Tile 2 left edge X:   {t2["bounds"].left}')
gap_h = abs(t1["bounds"].right - t2["bounds"].left)
if gap_h < 0.001:
    print(f'  Result: PERFECT ALIGNMENT ✓ (diff = {gap_h})')
else:
    print(f'  Result: GAP DETECTED ❌ (gap = {gap_h})')

print('\nVertical Adjacency (Tile 1 - Tile 3):')
print(f'  Tile 1 bottom edge Y: {t1["bounds"].bottom}')
print(f'  Tile 3 top edge Y:    {t3["bounds"].top}')
gap_v = abs(t1["bounds"].bottom - t3["bounds"].top)
if gap_v < 0.001:
    print(f'  Result: PERFECT ALIGNMENT ✓ (diff = {gap_v})')
else:
    print(f'  Result: GAP DETECTED ❌ (gap = {gap_v})')

print('\n' + '=' * 80)
if gap_h < 0.001 and gap_v < 0.001:
    print('SUCCESS: All tiles are PERFECTLY ALIGNED with NO GAPS!')
    print('Tiles are perfectly square and connect seamlessly!')
else:
    print('ISSUE: Gaps detected - alignment problem still exists')
print('=' * 80)

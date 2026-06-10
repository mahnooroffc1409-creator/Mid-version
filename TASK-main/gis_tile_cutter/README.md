# GIS Tile Cutter v2.0

Production-grade geospatial ML preprocessing platform for slicing multi-band GeoTIFF rasters
into configurable tiles with vegetation indices, feature extraction, and dataset splitting.

---

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

## Run

```bash
python main.py
```

---

## Key Features

### Auto Band Detection
Load an entire folder and bands are automatically mapped by filename patterns:

| Filename Pattern | Band Assignment |
|-----------------|-----------------|
| `red`, `b4`, `band4` | Red |
| `green`, `b3`, `band3` | Green |
| `nir`, `near_infrared`, `b5` | NIR |
| `rededge`, `re`, `b6` | RedEdge |
| `dem`, `dtm`, `elevation`, `dsm` | DEM |
| `blue`, `b2`, `band2` | Blue |

### Band Validation
- Automatic CRS mismatch detection across all loaded bands
- Metadata display: resolution, CRS, dimensions, datatype, NoData value
- Warning overlay when bands have inconsistent CRS

### AOI Statistics
Live statistics panel showing:
- Area (km²)
- Perimeter (m)
- Estimated tile count
- Estimated storage size (GB)

### Preview Modes
- **Single band** — view any loaded band
- **RGB composite** — Red + Green + NIR false-color
- **NDVI** — (NIR - Red) / (NIR + Red)
- **GNDVI** — (NIR - Green) / (NIR + Green)
- **NDRE** — (NIR - RedEdge) / (NIR + RedEdge)
- **SAVI** — Soil Adjusted Vegetation Index
- **DEM hillshade** — terrain visualization

### Histogram & Pixel Inspection
- Dynamic histogram with min/max/mean/std statistics
- Pixel value display on mouse hover for QA

### Tile Extraction
- **Overlap control** — configurable overlap in metres (essential for ML training with CNNs)
- **Edge handling** — discard, zero padding, or reflect padding
- **Tile naming** — row/col, geo coordinates, or timestamp-based
- **Memory-safe windowed reads** — only reads needed raster windows

### Feature Extraction
- Per-tile statistics: mean, std, min, max for each band
- Vegetation indices: NDVI, GNDVI, NDRE, SAVI
- Export as CSV for downstream analysis

### Dataset Splitting
- Automatic train/val/test split with configurable ratios
- Randomized with fixed seed for reproducibility
- Creates subdirectories with separate tile index files

### Processing Progress
- Real-time progress bar
- ETA display
- Active tile index tracking

---

## Workflow

1. **Load bands** — Click "Auto-detect & Load from Folder" or browse individual files
2. **Preview** — Select preview mode (single band, RGB, NDVI, etc.)
3. **Draw AOI** — Rectangle, polygon, freehand, or import shapefile/GeoJSON
4. **Configure tiles** — Set size, overlap, edge handling, and naming
5. **Enable features** — Check feature extraction and vegetation indices if needed
6. **Set split ratios** — Configure train/val/test percentages
7. **Export** — Click Export Tiles with live progress and ETA

---

## Output Structure

```
output_dir/
├── tile_r000_c000_Red.tif
├── tile_r000_c000_NIR.tif
├── tile_r000_c000_ALL4.tif
├── tile_index.geojson
├── tile_features.csv          ← if feature extraction enabled
├── train/
│   └── tile_index.geojson
├── val/
│   └── tile_index.geojson
└── test/
    └── tile_index.geojson
```

Each tile:
- Preserves original CRS and geotransform
- LZW-compressed GeoTIFF
- Compatible with QGIS, ArcGIS, rasterio, GDAL

---

## Notes

- **CRS**: Tiling uses auto-detected UTM zone of AOI centroid for accurate metre-based tiles
- **No data**: NoData values preserved in output tiles
- **Pan/zoom**: Right-drag or middle-drag to pan; scroll wheel to zoom
- **Cancel**: Exports can be cancelled mid-way — partial tiles are kept
- **Large rasters**: Windowed reading prevents memory issues with UAV mosaics

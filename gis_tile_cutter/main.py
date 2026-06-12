#!/usr/bin/env python3
"""
GIS Tile Cutter — Desktop App
Loads R / G / NIR / DEM GeoTIFFs, lets user select an AOI,
and exports 3.5×3.5 km tiles (single-band or all-4-stacked).
"""

import sys
import os
import math
import json
import time
import traceback
import re
from pathlib import Path
from datetime import datetime

import numpy as np
import rasterio
from rasterio.windows import from_bounds, Window
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.crs import CRS
from rasterio.warp import transform_bounds, reproject, Resampling
from rasterio.features import geometry_mask
from rasterio.mask import mask as rasterio_mask
from shapely.geometry import box, Polygon, mapping, shape
from shapely.ops import unary_union
from pyproj import Transformer

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QLabel, QPushButton, QFileDialog, QComboBox,
    QGroupBox, QGridLayout, QLineEdit, QSpinBox, QDoubleSpinBox,
    QProgressBar, QStatusBar, QTabWidget, QTextEdit, QCheckBox,
    QScrollArea, QFrame, QSizePolicy, QMessageBox, QToolButton,
    QButtonGroup, QRadioButton, QDialog, QPlainTextEdit,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRectF, QPointF, QTimer, QSize
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPixmap, QImage,
    QFont, QCursor, QPainterPath, QPolygonF
)
from PIL import Image
import cv2
import pandas as pd


# ── Band auto-detection patterns ─────────────────────────────────────────────
BAND_PATTERNS = [
    ("NIR",  [r"nir", r"near.?infrared", r"b5", r"band5", r"b8", r"band8"]),
    ("Red",  [r"red", r"b4", r"band4", r"r\b"]),
    ("Green",[r"green", r"b3", r"band3", r"g\b"]),
    ("Blue", [r"blue", r"b2", r"band2", r"b\b"]),
    ("RedEdge", [r"rededge", r"re", r"b6", r"band6", r"b7", r"band7", r"b8a"]),
    ("DEM",  [r"dem", r"dtm", r"elevation", r"height", r"dsm"]),
    ("SWIR1",[r"swir1", r"b11", r"band11"]),
    ("SWIR2",[r"swir2", r"b12", r"band12"]),
    ("Panchromatic", [r"pan", r"panchromatic"]),
]


def auto_detect_band(filename: str) -> str | None:
    """Detect band type from filename using pattern matching."""
    low = filename.lower().replace("_", " ").replace("-", " ").replace(".", " ")
    for band_name, patterns in BAND_PATTERNS:
        for pat in patterns:
            if re.search(rf"\b{pat}\b", low):
                return band_name
    return None


# ── Vegetation index computations ─────────────────────────────────────────────
def _clean_vi(arr: np.ndarray) -> np.ndarray:
    """Clean vegetation index by replacing NaN/Inf with 0 and converting to float32."""
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """NDVI = (NIR - Red) / (NIR + Red)"""
    denom = nir.astype(float) + red.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(denom != 0, (nir.astype(float) - red.astype(float)) / denom, 0.0)
    return _clean_vi(result)


def compute_gndvi(nir: np.ndarray, green: np.ndarray) -> np.ndarray:
    """GNDVI = (NIR - Green) / (NIR + Green), clipped to [-1, 1]"""
    nir_f = nir.astype(float)
    green_f = green.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (nir_f - green_f) / (nir_f + green_f)
    return np.clip(np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0), -1, 1).astype(np.float32)


def compute_ndre(nir: np.ndarray, rededge: np.ndarray) -> np.ndarray:
    """NDRE = (NIR - RedEdge) / (NIR + RedEdge)"""
    denom = nir.astype(float) + rededge.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(denom != 0, (nir.astype(float) - rededge.astype(float)) / denom, 0.0)
    return _clean_vi(result)


def compute_savi(nir: np.ndarray, red: np.ndarray, L=0.5) -> np.ndarray:
    """SAVI = ((NIR - Red) / (NIR + Red + L)) * (1 + L)"""
    denom = nir.astype(float) + red.astype(float) + L
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(denom != 0,
                        ((nir.astype(float) - red.astype(float)) / denom) * (1 + L), 0.0)
    return _clean_vi(result)


def compute_grvi(nir: np.ndarray, green: np.ndarray) -> np.ndarray:
    """GRVI = NIR / Green (ratio)"""
    nir_f = nir.astype(float)
    green_f = green.astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = nir_f / green_f
    return np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def compute_wrdvi(nir: np.ndarray, red: np.ndarray, alpha=0.1) -> np.ndarray:
    """WRDVI = (alpha * NIR - Red) / (alpha * NIR + Red), clipped to [-1, 1]"""
    nir_f = nir.astype(float)
    red_f = red.astype(float)
    anir = nir_f * alpha
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (anir - red_f) / (anir + red_f)
    return np.clip(np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0), -1, 1).astype(np.float32)


def compute_fcover(ndvi: np.ndarray, ndvi_soil=0.2, ndvi_veg=0.9) -> np.ndarray:
    """Fcover = (NDVI - NDVI_soil) / (NDVI_veg - NDVI_soil), clipped to [0, 1]"""
    if ndvi_veg <= ndvi_soil:
        return np.clip(ndvi, 0, 1).astype(np.float32)
    return np.clip((ndvi - ndvi_soil) / (ndvi_veg - ndvi_soil), 0, 1).astype(np.float32)


def compute_indices(bands: dict) -> dict[str, np.ndarray]:
    """Compute all available vegetation indices from loaded bands."""
    indices = {}
    nir = bands.get("NIR")
    red = bands.get("Red")
    green = bands.get("Green")
    rededge = bands.get("RedEdge")

    if nir is not None and red is not None:
        indices["NDVI"] = compute_ndvi(nir, red)
    if nir is not None and green is not None:
        indices["GNDVI"] = compute_gndvi(nir, green)
    if nir is not None and rededge is not None:
        indices["NDRE"] = compute_ndre(nir, rededge)
    if nir is not None and red is not None:
        indices["SAVI"] = compute_savi(nir, red)
    if nir is not None and rededge is not None:
        indices["WDRVI"] = compute_wrdvi(nir, rededge)
    if nir is not None and green is not None:
        indices["GRVI"] = compute_grvi(nir, green)
    if nir is not None and red is not None:
        indices["FCOVER"] = compute_fcover(indices.get("NDVI", compute_ndvi(nir, red)))
    return indices


def compute_rgb(red: np.ndarray, green: np.ndarray, blue: np.ndarray = None) -> np.ndarray:
    """Compute RGB composite. Uses Green as proxy for Blue if no Blue band."""
    if blue is None:
        blue = green
    def _norm(b):
        bmin, bmax = b.min(), b.max()
        if bmax == bmin:
            return np.zeros_like(b, dtype=np.uint8)
        return ((b - bmin) / (bmax - bmin) * 255).astype(np.uint8)
    return np.stack([_norm(red), _norm(green), _norm(blue)], axis=-1)


def compute_export_layers(bands: dict) -> dict[str, np.ndarray]:
    """Compute the default export layers."""
    layers = {}
    nir = bands.get("NIR")
    red = bands.get("Red")
    green = bands.get("Green")
    dem = bands.get("DEM")

    if nir is not None and red is not None and green is not None:
        layers["RGB"] = compute_rgb(red, green, bands.get("Blue"))
    if nir is not None and green is not None:
        layers["GNDVI"] = compute_gndvi(nir, green)
    if nir is not None and green is not None:
        layers["GRVI"] = compute_grvi(nir, green)
    if nir is not None and red is not None:
        layers["WDRVI"] = compute_wrdvi(nir, red)
    if dem is not None:
        layers["DEM"] = dem
    return layers


# ── AOI statistics ────────────────────────────────────────────────────────────
def compute_aoi_stats(aoi: Polygon, tile_m: float, src_crs,
                      overlap_m: float = 0.0,
                      edge_handling: str = "discard",
                      band_paths: dict | None = None,
                      band_indices: dict | None = None) -> dict:
    """Compute AOI area, perimeter, estimated tile count, and storage."""
    stats = {
        "area_m2": 0.0,
        "area_km2": 0.0,
        "perimeter_m": 0.0,
        "tile_count": 0,
        "est_size_gb": 0.0,
    }
    try:
        if src_crs.is_geographic:
            cx = (aoi.bounds[0] + aoi.bounds[2]) / 2
            cy = (aoi.bounds[1] + aoi.bounds[3]) / 2
            t = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
            lon, lat = t.transform(cx, cy)
            zone = int((lon + 180) / 6) + 1
            hemi = 326 if lat >= 0 else 327
            metric_crs = CRS.from_epsg(hemi * 100 + zone)
            t_fwd = Transformer.from_crs(src_crs, metric_crs, always_xy=True)
            aoi_metric = transform_polygon_crs(aoi, src_crs, metric_crs)
        else:
            aoi_metric = aoi
            metric_crs = src_crs

        stats["area_m2"] = aoi_metric.area
        stats["area_km2"] = aoi_metric.area / 1e6
        stats["perimeter_m"] = aoi_metric.length

        polys = compute_tile_polys(aoi, tile_m, src_crs, overlap_m, edge_handling,
                                   band_paths=band_paths, band_indices=band_indices)
        stats["tile_count"] = len(polys)

        est_bytes = stats["tile_count"] * (tile_m ** 2) * 4 * 4
        stats["est_size_gb"] = est_bytes / 1e9
    except Exception:
        pass
    return stats


def transform_polygon_crs(poly: Polygon, src_crs, dst_crs) -> Polygon:
    """Transform a polygon from one CRS to another."""
    t = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    coords = [(x, y) for x, y in poly.exterior.coords]
    if len(coords) < 3:
        return poly
    xs, ys = t.transform([c[0] for c in coords], [c[1] for c in coords])
    new_coords = list(zip(xs, ys))
    return Polygon(new_coords)


# ── CRS validation ────────────────────────────────────────────────────────────
def validate_band_crs(band_paths: dict) -> list[str]:
    """Check if all bands have the same CRS. Return list of warnings."""
    warnings = []
    crs_map = {}
    for bname, bpath in band_paths.items():
        try:
            with rasterio.open(bpath) as ds:
                crs_str = str(ds.crs)
                if crs_str not in crs_map:
                    crs_map[crs_str] = []
                crs_map[crs_str].append(bname)
        except Exception:
            warnings.append(f"Cannot read CRS for {bname}: {bpath}")
    if len(crs_map) > 1:
        crs_list = [f"{crs}: {', '.join(bands)}" for crs, bands in crs_map.items()]
        warnings.append("CRS MISMATCH — bands have different CRS:\n" + "\n".join(crs_list))
    return warnings


C_BG        = "#1e1e2e"
C_SURFACE   = "#2a2a3e"
C_PANEL     = "#252535"
C_ACCENT    = "#7c6af7"
C_ACCENT2   = "#5ec4b0"
C_WARN      = "#f0a23a"
C_ERR       = "#e05c5c"
C_TEXT      = "#d4d4e8"
C_MUTED     = "#7878a0"
C_BORDER    = "#3a3a5a"
C_SEL       = "#7c6af740"
C_TILE_LINE = "#7c6af7"
C_TILE_FILL = "#7c6af718"
C_AOI_LINE  = "#5ec4b0"
C_AOI_FILL  = "#5ec4b028"


# ── Stylesheet ────────────────────────────────────────────────────────────────
STYLE = f"""
QMainWindow, QWidget {{
    background: {C_BG};
    color: {C_TEXT};
    font-family: 'Segoe UI', 'Inter', sans-serif;
    font-size: 13px;
}}
QGroupBox {{
    border: 1px solid {C_BORDER};
    border-radius: 8px;
    margin-top: 12px;
    padding: 8px;
    font-weight: 600;
    color: {C_TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {C_ACCENT};
}}
QPushButton {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    color: {C_TEXT};
}}
QPushButton:hover {{ background: {C_ACCENT}; color: white; border-color: {C_ACCENT}; }}
QPushButton:pressed {{ background: #5a4ed4; }}
QPushButton:disabled {{ color: {C_MUTED}; }}
QPushButton#primary {{
    background: {C_ACCENT};
    color: white;
    border-color: {C_ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: #6a58e8; }}
QPushButton#danger {{ background: {C_ERR}; color: white; border-color: {C_ERR}; }}
QPushButton:checked {{
    background: {C_ACCENT};
    color: white;
    border-color: {C_ACCENT};
}}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    border-radius: 5px;
    padding: 4px 8px;
    color: {C_TEXT};
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {C_ACCENT};
}}
QComboBox::drop-down {{ border: none; }}
QComboBox QAbstractItemView {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    selection-background-color: {C_ACCENT};
}}
QProgressBar {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    border-radius: 5px;
    height: 14px;
    text-align: center;
    color: {C_TEXT};
}}
QProgressBar::chunk {{ background: {C_ACCENT}; border-radius: 4px; }}
QTabWidget::pane {{
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    background: {C_PANEL};
}}
QTabBar::tab {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    border-bottom: none;
    border-radius: 5px 5px 0 0;
    padding: 5px 14px;
    color: {C_MUTED};
}}
QTabBar::tab:selected {{ background: {C_PANEL}; color: {C_TEXT}; }}
QTextEdit {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    color: {C_TEXT};
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12px;
}}
QScrollBar:vertical {{
    background: {C_SURFACE};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    border-radius: 4px;
    min-height: 20px;
}}
QCheckBox {{ color: {C_TEXT}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    background: {C_SURFACE};
}}
QCheckBox::indicator:checked {{
    background: {C_ACCENT};
    border-color: {C_ACCENT};
}}
QRadioButton {{ color: {C_TEXT}; spacing: 6px; }}
QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {C_BORDER};
    border-radius: 7px;
    background: {C_SURFACE};
}}
QRadioButton::indicator:checked {{
    background: {C_ACCENT};
    border-color: {C_ACCENT};
}}
QSplitter::handle {{ background: {C_BORDER}; }}
QLabel#section {{ color: {C_ACCENT}; font-weight: 600; font-size: 11px; text-transform: uppercase; }}
QLabel#coord {{ font-family: 'Consolas', monospace; color: {C_ACCENT2}; font-size: 11px; }}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Map Canvas
# ══════════════════════════════════════════════════════════════════════════════

class MapCanvas(QWidget):
    aoi_changed = pyqtSignal(object)
    coord_moved = pyqtSignal(float, float)
    pixel_inspected = pyqtSignal(object, object)

    TOOL_RECT = 0
    TOOL_POLY = 1
    TOOL_FREE = 2
    TOOL_PAN  = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(500, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.overview: QPixmap | None = None
        self.raster_bounds = None
        self.raster_crs    = None
        self.dataset_width = 0
        self.dataset_height = 0

        self.offset  = QPointF(0, 0)
        self.scale   = 1.0

        self.tool = self.TOOL_RECT
        self._pan_start = None
        self._pan_offset_start = None

        self._rect_start: QPointF | None = None
        self._rect_cur:   QPointF | None = None
        self._poly_pts:   list[QPointF]  = []
        self._free_pts:   list[QPointF]  = []
        self._dragging_free = False

        self.aoi_screen: list[QPointF] = []
        self.aoi_geo:    Polygon | None = None

        self.tile_polys_screen: list[list[QPointF]] = []
        self.tile_geo: list[Polygon] = []

        self.show_aoi_fill  = False
        self.show_tile_fill = True

    def load_overview(self, pixmap: QPixmap, bounds, crs):
        self.overview = pixmap
        self.raster_bounds = bounds
        self.raster_crs = crs
        self.aoi_screen = []
        self.aoi_geo = None
        self.tile_polys_screen = []
        self.tile_geo = []
        self._fit_view()
        self.update()

    def _fit_view(self):
        if self.overview is None:
            return
        w, h = self.width(), self.height()
        iw, ih = self.overview.width(), self.overview.height()
        self.scale = min(w / iw, h / ih) * 0.95
        self.offset = QPointF(0, 0)

    def resizeEvent(self, e):
        self._fit_view()
        self._rebuild_tile_overlay()
        self._rebuild_aoi_overlay()
        super().resizeEvent(e)

    def reset_view(self):
        self._fit_view()
        self.update()

    def set_tile_grid(self, tile_geo: list[Polygon]):
        self.tile_geo = tile_geo
        self._rebuild_tile_overlay()
        self.update()

    def clear_tiles(self):
        self.tile_geo = []
        self.tile_polys_screen = []
        self.update()

    def _geo_to_screen(self, gx, gy) -> QPointF:
        if self.overview is None or self.raster_bounds is None:
            return QPointF(0, 0)
        iw, ih = self.overview.width(), self.overview.height()
        left, bottom, right, top = self.raster_bounds
        x_range = right - left
        y_range = top - bottom
        if x_range == 0 or y_range == 0:
            return QPointF(0, 0)
        ix = ((gx - left) / x_range) * iw
        iy = ((top - gy) / y_range) * ih
        cw, ch = self.width(), self.height()
        cx = cw / 2 + self.offset.x()
        cy = ch / 2 + self.offset.y()
        sx = cx + (ix - iw / 2) * self.scale
        sy = cy + (iy - ih / 2) * self.scale
        return QPointF(sx, sy)

    def _rebuild_tile_overlay(self):
        if not self.tile_geo:
            return
        self.tile_polys_screen = []
        for poly in self.tile_geo:
            pts = [self._geo_to_screen(x, y) for x, y in poly.exterior.coords]
            self.tile_polys_screen.append(pts)

    def _rebuild_aoi_overlay(self):
        if self.aoi_geo is None:
            return
        self.aoi_screen = [self._geo_to_screen(x, y)
                           for x, y in self.aoi_geo.exterior.coords]

    def _to_raster(self, pos: QPointF):
        if self.overview is None:
            return None, None
        cw, ch = self.width(), self.height()
        iw, ih = self.overview.width(), self.overview.height()
        cx = cw / 2 + self.offset.x()
        cy = ch / 2 + self.offset.y()
        rx = (pos.x() - cx) / self.scale + iw / 2
        ry = (pos.y() - cy) / self.scale + ih / 2
        return rx, ry

    def _geo_from_screen(self, pos: QPointF):
        if self.overview is None or self.raster_bounds is None:
            return None, None
        rx, ry = self._to_raster(pos)
        if rx is None:
            return None, None
        w = self.overview.width()
        h = self.overview.height()
        left, bottom, right, top = self.raster_bounds
        lon = left + (rx / w) * (right - left)
        lat = bottom + (1 - ry / h) * (top - bottom)
        return lon, lat

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cw, ch = self.width(), self.height()
        p.fillRect(0, 0, cw, ch, QColor(C_BG))

        if self.overview is None:
            p.setPen(QColor(C_MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No raster loaded")
            return

        iw, ih = self.overview.width(), self.overview.height()
        cx = cw / 2 + self.offset.x()
        cy = ch / 2 + self.offset.y()
        sw = iw * self.scale
        sh = ih * self.scale
        p.drawPixmap(int(cx - sw / 2), int(cy - sh / 2), int(sw), int(sh), self.overview)

        # tile grid
        if self.show_tile_fill:
            p.setBrush(QBrush(QColor(C_TILE_FILL)))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(C_TILE_LINE), 1))
        for poly_pts in self.tile_polys_screen:
            if len(poly_pts) < 3:
                continue
            path = QPainterPath()
            path.moveTo(poly_pts[0])
            for pt in poly_pts[1:]:
                path.lineTo(pt)
            path.closeSubpath()
            p.drawPath(path)

        # AOI
        if self.aoi_screen:
            if self.show_aoi_fill:
                p.setBrush(QBrush(QColor(C_AOI_FILL)))
            else:
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C_AOI_LINE), 2))
            path = QPainterPath()
            path.moveTo(self.aoi_screen[0])
            for pt in self.aoi_screen[1:]:
                path.lineTo(pt)
            path.closeSubpath()
            p.drawPath(path)
            # vertices
            p.setBrush(QBrush(QColor(C_AOI_LINE)))
            for pt in self.aoi_screen:
                p.drawEllipse(pt, 4, 4)

        # drawing state
        if self.tool == self.TOOL_RECT and self._rect_start and self._rect_cur:
            p.setPen(QPen(QColor(C_AOI_LINE), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = QRectF(self._rect_start, self._rect_cur)
            p.drawRect(r)

        if self.tool == self.TOOL_POLY and self._poly_pts:
            p.setPen(QPen(QColor(C_AOI_LINE), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(len(self._poly_pts) - 1):
                p.drawLine(self._poly_pts[i], self._poly_pts[i + 1])

    def mousePressEvent(self, e):
        self.setFocus()
        if self.overview is None:
            return
        pos = e.position()
        if self.tool == self.TOOL_PAN:
            self._pan_start = pos
            self._pan_offset_start = QPointF(self.offset)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if e.button() == Qt.MouseButton.LeftButton:
            if self.tool == self.TOOL_RECT:
                self._rect_start = pos
                self._rect_cur = pos
            elif self.tool == self.TOOL_POLY:
                self._poly_pts.append(pos)
                if len(self._poly_pts) >= 3:
                    lon, lat = self._geo_from_screen(pos)
                    if lon is not None:
                        self.pixel_inspected.emit(lon, lat)
            elif self.tool == self.TOOL_FREE:
                self._free_pts = [pos]
                self._dragging_free = True

    def mouseMoveEvent(self, e):
        pos = e.position()
        lon, lat = self._geo_from_screen(pos)
        if lon is not None:
            self.coord_moved.emit(lon, lat)
            self.pixel_inspected.emit(lon, lat)

        if self.tool == self.TOOL_PAN and self._pan_start is not None:
            dx = pos.x() - self._pan_start.x()
            dy = pos.y() - self._pan_start.y()
            self.offset = self._pan_offset_start + QPointF(dx, dy)
            self.update()
            return

        if self.tool == self.TOOL_RECT and self._rect_start is not None:
            self._rect_cur = pos
            self.update()

        if self.tool == self.TOOL_FREE and self._dragging_free:
            self._free_pts.append(pos)
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.tool == self.TOOL_PAN and self._pan_start is not None:
            self._pan_start = None
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            return

        if self.tool == self.TOOL_RECT and self._rect_start is not None and self._rect_cur is not None:
            r = QRectF(self._rect_start, self._rect_cur)
            self.aoi_screen = [
                r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()
            ]
            self._commit_aoi()
            self._rect_start = None
            self._rect_cur = None
            self.update()

        if self.tool == self.TOOL_POLY:
            if len(self._poly_pts) >= 3:
                self.aoi_screen = list(self._poly_pts)
                self._commit_aoi()
                self._poly_pts = []
                self.update()

        if self.tool == self.TOOL_FREE and self._dragging_free:
            self._dragging_free = False
            if len(self._free_pts) >= 3:
                self.aoi_screen = list(self._free_pts)
                self._commit_aoi()
                self._free_pts = []
                self.update()

    def _commit_aoi(self):
        """Convert screen AOI to geographic coordinates."""
        geo_pts = []
        for pt in self.aoi_screen:
            lon, lat = self._geo_from_screen(pt)
            if lon is not None:
                geo_pts.append((lon, lat))
        if len(geo_pts) >= 3:
            self.aoi_geo = Polygon(geo_pts)
            self.aoi_changed.emit(self.aoi_geo)

    def clear_aoi(self):
        self.aoi_screen = []
        self.aoi_geo = None
        self.tile_polys_screen = []
        self._rect_start = None
        self._rect_cur = None
        self._poly_pts = []
        self._free_pts = []
        self.update()
        self.aoi_changed.emit(None)

    def _rebuild_overlays(self):
        self._rebuild_tile_overlay()
        self._rebuild_aoi_overlay()

    def wheelEvent(self, e):
        self.setFocus()
        delta = e.angleDelta().y()
        if delta > 0:
            self.scale *= 1.15
        elif delta < 0:
            self.scale /= 1.15
        self.scale = max(self.scale, 0.05)
        self._rebuild_overlays()
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self._dragging_free:
                self._dragging_free = False
                self._free_pts = []
            self._rect_start = None
            self._rect_cur = None
            self._poly_pts = []
            self.update()
        elif e.key() == Qt.Key.Key_Delete or e.key() == Qt.Key.Key_Backspace:
            self.clear_aoi()
        elif e.key() == Qt.Key.Key_Plus or e.key() == Qt.Key.Key_Equal:
            self.scale *= 1.25
            self._rebuild_overlays()
            self.update()
        elif e.key() == Qt.Key.Key_Minus:
            self.scale /= 1.25
            self._rebuild_overlays()
            self.update()


# ══════════════════════════════════════════════════════════════════════════════
# Histogram Widget
# ══════════════════════════════════════════════════════════════════════════════

class HistogramWidget(QWidget):
    """Simple histogram display."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self._data: list[np.ndarray] = []
        self._labels: list[str] = []
        self._colors: list[str] = []

    def set_histogram(self, data: list[np.ndarray], labels: list[str], colors: list[str]):
        self._data = data
        self._labels = labels
        self._colors = colors
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C_PANEL))
        if not self._data:
            p.setPen(QColor(C_MUTED))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No data")
            return

        margin = 40
        pw = w - 2 * margin
        ph = h - 2 * margin
        bins = 100
        bin_w = max(pw / bins, 1)

        for idx, data in enumerate(self._data):
            if data.size == 0:
                continue
            hist, edges = np.histogram(data, bins=bins)
            if hist.max() == 0:
                continue
            hist_n = hist / hist.max()
            p.setPen(QColor(self._colors[idx] if idx < len(self._colors) else C_ACCENT))
            for i in range(bins):
                x = margin + i * bin_w
                bh = hist_n[i] * ph
                p.drawLine(int(x), int(h - margin), int(x), int(h - margin - bh))

    def minimumSizeHint(self):
        return QSize(200, 180)


# ══════════════════════════════════════════════════════════════════════════════
# Band Info Widget
# ══════════════════════════════════════════════════════════════════════════════

class BandInfoWidget(QWidget):
    """Display loaded band metadata."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(2)
        self._labels = {}

    def set_info(self, bands: dict):
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        self._labels = {}
        if not bands:
            self._layout.addWidget(QLabel("No bands loaded"))
            return
        for bname, bpath in bands.items():
            try:
                with rasterio.open(bpath) as ds:
                    txt = (
                        f"<b>{bname}</b><br>"
                        f"  Path: {Path(bpath).name}<br>"
                        f"  Size: {ds.width} x {ds.height}<br>"
                        f"  CRS: {ds.crs}<br>"
                        f"  Dtype: {ds.dtypes[0]}<br>"
                        f"  Nodata: {ds.nodata}<br>"
                        f"  Resolution: {abs(ds.res[0]):.4f}, {abs(ds.res[1]):.4f}"
                    )
            except Exception as e:
                txt = f"<b>{bname}</b><br>  Error: {e}"
            lbl = QLabel(txt)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{C_TEXT};font-size:11px;padding:4px;border-bottom:1px solid {C_BORDER};")
            self._layout.addWidget(lbl)


# ══════════════════════════════════════════════════════════════════════════════
# AOI Stats Widget
# ══════════════════════════════════════════════════════════════════════════════

class AOIStatsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        l = QVBoxLayout(self)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(2)
        self.area_lbl = QLabel("Area: —")
        self.area_lbl.setObjectName("coord")
        l.addWidget(self.area_lbl)
        self.perim_lbl = QLabel("Perimeter: —")
        self.perim_lbl.setObjectName("coord")
        l.addWidget(self.perim_lbl)
        self.est_gb_lbl = QLabel("Est. size: —")
        self.est_gb_lbl.setObjectName("coord")
        l.addWidget(self.est_gb_lbl)

    def update_stats(self, stats: dict):
        self.area_lbl.setText(f"Area: {stats['area_km2']:.3f} km²")
        self.perim_lbl.setText(f"Perimeter: {stats['perimeter_m']:.0f} m")
        self.est_gb_lbl.setText(f"Est. size: {stats['est_size_gb']:.2f} GB")

# ══════════════════════════════════════════════════════════════════════════════
# Band Loader Worker
# ══════════════════════════════════════════════════════════════════════════════

class BandLoader(QThread):
    done = pyqtSignal(str, object, object, int, int, int, int)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    MAX_PREVIEW_DIM = 2048

    def __init__(self, path: str, band_index: int = 1):
        super().__init__()
        self.path = path
        self.band_index = band_index

    def run(self):
        try:
            with rasterio.open(self.path) as src:
                orig_w, orig_h = src.width, src.height
                scale = min(self.MAX_PREVIEW_DIM / orig_w, self.MAX_PREVIEW_DIM / orig_h, 1.0)
                pw = max(int(orig_w * scale), 1)
                ph = max(int(orig_h * scale), 1)
                data = src.read(self.band_index, out_shape=(ph, pw),
                                resampling=Resampling.average).astype(np.float32)
                nodata = src.nodata
                if nodata is not None:
                    data = np.where(data == nodata, np.nan, data)
                bounds = src.bounds
                crs = src.crs
            self.done.emit(self.path, data, bounds, orig_w, orig_h, pw, ph)
        except Exception as e:
            self.error.emit(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Tile grid calculator
# ══════════════════════════════════════════════════════════════════════════════

def compute_tile_polys(aoi_geo: Polygon, tile_m: float, src_crs,
                        overlap_m: float = 0.0, edge_handling: str = "discard",
                        band_paths: dict | None = None,
                        band_indices: dict | None = None
                        ) -> list[Polygon]:
    """Return tile box polygons in src_crs that intersect the AOI.
    Uses UTM metric grid matching TilingWorker."""
    aoi_bounds = aoi_geo.bounds
    cx = (aoi_bounds[0] + aoi_bounds[2]) / 2
    cy = (aoi_bounds[1] + aoi_bounds[3]) / 2
    if not src_crs.is_geographic:
        t = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
        lon, lat = t.transform(cx, cy)
    else:
        lon, lat = cx, cy
    zone = int((lon + 180) / 6) + 1
    hemi = 326 if lat >= 0 else 327
    metric_crs = CRS.from_epsg(hemi * 100 + zone)

    t_fwd = Transformer.from_crs(src_crs, metric_crs, always_xy=True)
    t_inv = Transformer.from_crs(metric_crs, src_crs, always_xy=True)
    xs, ys = t_fwd.transform(
        [aoi_bounds[0], aoi_bounds[2]],
        [aoi_bounds[1], aoi_bounds[3]]
    )
    m_minx, m_maxx = min(xs), max(xs)
    m_miny, m_maxy = min(ys), max(ys)

    stride = tile_m
    cols = math.ceil((m_maxx - m_minx - tile_m) / stride) + 1
    rows = math.ceil((m_maxy - m_miny - tile_m) / stride) + 1
    polys = []
    for r in range(rows):
        for c in range(cols):
            t_minx = m_minx + c * stride
            t_miny = m_miny + r * stride
            t_maxx = min(t_minx + tile_m, m_minx + (cols - 1) * stride + tile_m)
            t_maxy = min(t_miny + tile_m, m_miny + (rows - 1) * stride + tile_m)
            xs2, ys2 = t_inv.transform(
                [t_minx, t_maxx], [t_miny, t_maxy])
            s_minx, s_maxx = min(xs2), max(xs2)
            s_miny, s_maxy = min(ys2), max(ys2)
            tile_box = box(s_minx, s_miny, s_maxx, s_maxy)
            if aoi_geo.intersects(tile_box):
                polys.append(tile_box)
    return polys



# ══════════════════════════════════════════════════════════════════════════════
# Tiling Worker
# ══════════════════════════════════════════════════════════════════════════════

class TilingWorker(QThread):
    progress = pyqtSignal(int, int, str)
    tile_done = pyqtSignal(str)
    finished = pyqtSignal(int, str)
    error = pyqtSignal(str)

    def __init__(self, band_paths=None, aoi_geo=None, tile_m=100,
                 output_dir="", export_layers=None,
                 overlap=0.0, edge_handling="discard",
                 band_indices=None, tile_naming="rowcol"):
        super().__init__()
        self.band_paths = band_paths or {}
        self.aoi_geo = aoi_geo
        self.tile_m = tile_m
        self.output_dir = output_dir
        self.export_layers = export_layers or []
        self.overlap = overlap
        self.edge_handling = edge_handling
        self.band_indices = band_indices or {}
        self.tile_naming = tile_naming
        self._cancelled = False
        self._start_time = 0

    def cancel(self):
        self._cancelled = True

    def _tile_filename(self, r: int, c: int, band_label: str = "", suffix: str = "") -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.tile_naming == "timestamp":
            base = f"{ts}_r{r:04d}_c{c:04d}"
        elif self.tile_naming == "geo":
            base = f"x{c:04d}_y{r:04d}"
        else:
            base = f"r{r:03d}_c{c:03d}"
        parts = [base]
        if band_label:
            parts.append(band_label)
        if suffix:
            parts.append(suffix)
        return "_".join(parts)

    def run(self):
        try:
            out = Path(self.output_dir)
            out.mkdir(parents=True, exist_ok=True)

            # Use a spectral band as reference (skip DEM which may have different resolution)
            ref_band = next((b for b in self.band_paths if b != "DEM"), next(iter(self.band_paths)))
            first_path = self.band_paths[ref_band]
            with rasterio.open(first_path) as ref:
                src_crs = ref.crs
                raster_transform = ref.transform
                raster_width = ref.width
                raster_height = ref.height

            aoi = self.aoi_geo
            common_bounds = None
            for bpath in self.band_paths.values():
                with rasterio.open(bpath) as src:
                    b = box(*src.bounds)
                    common_bounds = b if common_bounds is None else common_bounds.intersection(b)
            if common_bounds is not None and not common_bounds.is_empty:
                clipped_aoi = aoi.intersection(common_bounds)
                if not clipped_aoi.is_empty:
                    aoi = clipped_aoi

            aoi_b = aoi.bounds

            pixel_size_x = abs(raster_transform.a)
            pixel_size_y = abs(raster_transform.e)

            if src_crs.is_geographic:
                cx = (aoi_b[0] + aoi_b[2]) / 2
                cy = (aoi_b[1] + aoi_b[3]) / 2
                t = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
                lon, lat = t.transform(cx, cy)
                m_per_deg = 111320.0 * math.cos(math.radians(lat))
                tile_m = self.tile_m / m_per_deg
            else:
                tile_m = self.tile_m
            tile_width = max(int(round(tile_m / pixel_size_x)), 1)
            tile_height = max(int(round(tile_m / pixel_size_y)), 1)

            # v1 pixel grid: align tile starts to file origin
            win_aoi = from_bounds(aoi_b[0], aoi_b[1], aoi_b[2], aoi_b[3], raster_transform)

            p_start_col = max(0, int(math.floor(win_aoi.col_off)))
            p_start_row = max(0, int(math.floor(win_aoi.row_off)))
            p_end_col = min(raster_width, int(math.ceil(win_aoi.col_off + win_aoi.width)))
            p_end_row = min(raster_height, int(math.ceil(win_aoi.row_off + win_aoi.height)))

            # Snap start to tile_width/tile_height boundaries from file origin
            tile_start_col = (p_start_col // tile_width) * tile_width
            tile_start_row = (p_start_row // tile_height) * tile_height

            tiles = []
            for row in range(tile_start_row, p_end_row, tile_height):
                for col in range(tile_start_col, p_end_col, tile_width):
                    tw = min(tile_width, raster_width - col)
                    th = min(tile_height, raster_height - row)
                    if tw < 1 or th < 1:
                        continue
                    geo_bounds = rasterio.windows.bounds(Window(col, row, tw, th), raster_transform)
                    tile_box = box(*geo_bounds)
                    if aoi.intersects(tile_box):
                        r_idx = (row - tile_start_row) // tile_height
                        c_idx = (col - tile_start_col) // tile_width
                        tiles.append((r_idx, c_idx, col, row, tw, th, tile_box, geo_bounds, raster_transform))
                    if self._cancelled:
                        return

            grid_total = len(tiles)
            if grid_total > 5000:
                self.error.emit(f"Too many tiles ({grid_total}). Reduce AOI or increase tile size.")
                return

            self.progress.emit(0, 0,
                f"Grid: {tile_width}x{tile_height}px tiles, "
                f"AOI pixel [{p_start_col}:{p_end_col}]x[{p_start_row}:{p_end_row}], "
                f"{grid_total} tiles")

            total_tiles = len(tiles)
            tile_index = []
            self._start_time = time.time()

            for idx, (r_idx, c_idx, px_col, px_row, tw, th, tile_box, geo_bounds, src_t) in enumerate(tiles, 1):
                if self._cancelled:
                    return

                elapsed = time.time() - self._start_time
                eta = (elapsed / idx) * (total_tiles - idx) if idx > 0 else 0
                eta_str = f"{int(eta // 60)}m {int(eta % 60)}s"
                label = self._tile_filename(r_idx, c_idx)
                msg = f"Tile {label} ({idx}/{total_tiles}) ETA:{eta_str}"
                self.progress.emit(idx, total_tiles, msg)

                out_transform = rasterio.windows.transform(
                    Window(px_col, px_row, tw, th), src_t)

                # Read each band using the same pixel window (all bands share pixel grid)
                tile_bands = {}
                tile_meta = {}
                for bname, bpath in self.band_paths.items():
                    band_idx = self.band_indices.get(bname, 1)
                    try:
                        with rasterio.open(bpath) as src:
                            # Convert pixel window to geographic bounds for this file
                            tile_bounds = rasterio.windows.bounds(
                                Window(px_col, px_row, tw, th), raster_transform)
                            b_win = from_bounds(*tile_bounds, src.transform)
                            b_win = b_win.round_offsets().round_shape()
                            b_win = b_win.intersection(Window(0, 0, src.width, src.height))
                            if b_win.width >= 1 and b_win.height >= 1:
                                raw = src.read(band_idx, window=b_win)
                                orig_dtype = raw.dtype
                                expected_h = max(1, int(round(b_win.height)))
                                expected_w = max(1, int(round(b_win.width)))
                                if raw.shape != (expected_h, expected_w):
                                    full = np.full((expected_h, expected_w), np.nan, dtype=np.float32)
                                    rh = min(raw.shape[0], expected_h)
                                    rw = min(raw.shape[1], expected_w)
                                    full[:rh, :rw] = raw[:rh, :rw].astype(np.float32)
                                    raw = full
                            else:
                                orig_dtype = src.dtypes[0] if src.dtypes else np.float32
                                raw = np.full((1, 1), np.nan, dtype=np.float32)
                            nodata_val = src.nodata
                            data_float = raw if raw.dtype == np.float32 else raw.astype(np.float32)
                            if nodata_val is not None and not np.isnan(nodata_val):
                                data_float[data_float == nodata_val] = np.nan
                            tile_bands[bname] = data_float
                            tile_meta[bname] = (orig_dtype, nodata_val)
                    except Exception as e:
                        self.progress.emit(0, 0, f"ERROR reading {bname}: {e}")

                if not tile_bands:
                    continue

                tile_index.append({
                    "type": "Feature",
                    "geometry": mapping(tile_box),
                    "properties": {"tile": label, "row": px_row, "col": px_col}
                })

                for layer in self.export_layers:
                    layer_dir = out / layer
                    layer_dir.mkdir(parents=True, exist_ok=True)
                    fname = self._tile_filename(r_idx, c_idx)
                    out_path = layer_dir / f"tile_{fname}.tif"

                    if layer == "RGB":
                        red = tile_bands.get("Red")
                        green = tile_bands.get("Green")
                        blue = tile_bands.get("Blue")
                        if red is not None and green is not None:
                            if blue is None:
                                blue = green
                            red = np.where(np.isnan(red), 0, red)
                            green = np.where(np.isnan(green), 0, green)
                            blue = np.where(np.isnan(blue), 0, blue)
                            def _norm(b):
                                bmin, bmax = b.min(), b.max()
                                if bmax == bmin:
                                    return np.zeros_like(b, dtype=np.uint8)
                                return ((b - bmin) / (bmax - bmin) * 255).astype(np.uint8)
                            stacked = np.stack([_norm(red), _norm(green), _norm(blue)])
                            profile = {
                                "driver": "GTiff", "count": 3,
                                "width": tw, "height": th,
                                "transform": out_transform, "compress": "lzw",
                                "dtype": "uint8", "crs": src_crs,
                            }
                            with rasterio.open(str(out_path), "w", **profile) as dst:
                                for bi in range(3):
                                    dst.write(stacked[bi], bi + 1)
                                dst.update_tags(BAND_NAMES="Red,Green,Blue")
                            self.tile_done.emit(str(out_path))

                    elif layer == "GNDVI":
                        nir = tile_bands.get("NIR")
                        green = tile_bands.get("Green")
                        if nir is not None and green is not None:
                            h = min(nir.shape[0], green.shape[0])
                            w = min(nir.shape[1], green.shape[1])
                            arr = compute_gndvi(nir[:h, :w], green[:h, :w])
                            vi_f32 = arr.astype(np.float32)
                            self._write_tile_window(vi_f32, out_path, out_transform, src_crs,
                                                   dtype_override="float32",
                                                   tags={"VI_FORMULA": "GNDVI", "VI_RANGE_MIN": "-1", "VI_RANGE_MAX": "1"})

                    elif layer == "GRVI":
                        nir = tile_bands.get("NIR")
                        green = tile_bands.get("Green")
                        if nir is not None and green is not None:
                            h = min(nir.shape[0], green.shape[0])
                            w = min(nir.shape[1], green.shape[1])
                            arr = compute_grvi(nir[:h, :w], green[:h, :w])
                            vi_f32 = arr.astype(np.float32)
                            self._write_tile_window(vi_f32, out_path, out_transform, src_crs,
                                                   dtype_override="float32",
                                                   tags={"VI_FORMULA": "GRVI", "VI_RANGE_MIN": "0", "VI_RANGE_MAX": "inf"})

                    elif layer == "WDRVI":
                        nir = tile_bands.get("NIR")
                        red = tile_bands.get("Red")
                        if nir is not None and red is not None:
                            h = min(nir.shape[0], red.shape[0])
                            w = min(nir.shape[1], red.shape[1])
                            arr = compute_wrdvi(nir[:h, :w], red[:h, :w])
                            vi_f32 = arr.astype(np.float32)
                            self._write_tile_window(vi_f32, out_path, out_transform, src_crs,
                                                   dtype_override="float32",
                                                   tags={"VI_FORMULA": "WDRVI", "VI_RANGE_MIN": "-1", "VI_RANGE_MAX": "1"})

                    elif layer == "DEM":
                        dem = tile_bands.get("DEM")
                        if dem is not None:
                            # Compute transform from tile geographic bounds and DEM array shape
                            tile_bounds = rasterio.windows.bounds(
                                Window(px_col, px_row, tw, th), raster_transform)
                            dem_transform = transform_from_bounds(
                                *tile_bounds, dem.shape[1], dem.shape[0])
                            dt, nod = tile_meta.get("DEM", (np.float32, None))
                            has_nan = np.isnan(dem).any()
                            if has_nan:
                                dem_out = np.where(np.isnan(dem), 0, dem).astype(np.float32)
                                self._write_tile_window(dem_out, out_path, dem_transform, src_crs)
                            else:
                                dem_out = dem.astype(dt) if dt != np.float32 else dem
                                self._write_tile_window(dem_out, out_path, dem_transform, src_crs, dtype_override=dt.name if dt != np.float32 else None, nodata_override=nod)

                # Always export individual band tiles
                band_dir = out / "bands"
                band_dir.mkdir(parents=True, exist_ok=True)
                tile_bounds = rasterio.windows.bounds(
                    Window(px_col, px_row, tw, th), raster_transform)
                for bname, data_float in tile_bands.items():
                    if data_float is not None:
                        orig_dtype, orig_nodata = tile_meta.get(bname, (np.float32, None))
                        # Compute transform matching this band's data array shape
                        band_transform = transform_from_bounds(
                            *tile_bounds, data_float.shape[1], data_float.shape[0])
                        has_nan = np.isnan(data_float).any()
                        fname = self._tile_filename(r_idx, c_idx, band_label=bname)
                        if has_nan:
                            if np.issubdtype(orig_dtype, np.integer):
                                fill = orig_nodata if orig_nodata is not None else 0
                                out_data = np.where(np.isnan(data_float), fill, data_float).astype(orig_dtype)
                                self._write_tile_window(out_data, band_dir / f"tile_{fname}.tif", band_transform, src_crs, dtype_override=orig_dtype.name, nodata_override=fill if isinstance(fill, (int, float)) else None)
                            else:
                                out_data = np.where(np.isnan(data_float), 0, data_float).astype(np.float32)
                                self._write_tile_window(out_data, band_dir / f"tile_{fname}.tif", band_transform, src_crs)
                        else:
                            out_data = data_float.astype(orig_dtype) if orig_dtype != np.float32 else data_float
                            self._write_tile_window(out_data, band_dir / f"tile_{fname}.tif", band_transform, src_crs, dtype_override=orig_dtype.name if orig_dtype != np.float32 else None, nodata_override=orig_nodata)

            geojson = {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": str(src_crs)}},
                "features": tile_index
            }
            with open(out / "tile_index.geojson", "w") as f:
                json.dump(geojson, f, indent=2)

            total_written = len(tile_index)
            self.finished.emit(total_written, str(out))

        except Exception:
            self.error.emit(traceback.format_exc())

    def _write_tile_window(self, data, out_path, transform, crs, dtype_override=None, nodata_override=None, tags=None):
        dt = dtype_override or data.dtype.name
        profile = {
            "driver": "GTiff",
            "count": 1,
            "width": data.shape[1],
            "height": data.shape[0],
            "transform": transform,
            "compress": "lzw",
            "dtype": dt,
            "crs": crs,
        }
        if nodata_override is not None:
            profile["nodata"] = nodata_override
        else:
            has_nan = np.isnan(data).any()
            if has_nan:
                profile["nodata"] = np.nan
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)
            if tags:
                dst.update_tags(**tags)
        self.tile_done.emit(str(out_path))


# ══════════════════════════════════════════════════════════════════════════════
# Main Window
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GIS Tile Cutter")
        self.setMinimumSize(1200, 700)
        self.setStyleSheet(STYLE)

        self.band_paths: dict[str, str] = {}
        self.band_indices: dict[str, int] = {}
        self.band_data: dict[str, np.ndarray] = {}
        self._raster_bounds = None
        self._raster_crs = None
        self._dataset_width = 0
        self._dataset_height = 0

        self._loader: BandLoader | None = None
        self._worker: TilingWorker | None = None
        self._extract_worker = None
        self._vigor_ml_window = None

        self._setup_ui()

        QTimer.singleShot(0, self.canvas.setFocus)
        self.status("Ready — load band GeoTIFFs to begin")

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_h = QHBoxLayout(central)
        main_h.setSpacing(0)
        main_h.setContentsMargins(0, 0, 0, 0)

        # ── Left panel ──
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setMaximumWidth(380)
        left_w = QWidget()
        lv = QVBoxLayout(left_w)
        lv.setSpacing(8)
        lv.setContentsMargins(10, 10, 10, 10)

        # Input bands section
        bg = QGroupBox("Input bands")
        bg_v = QVBoxLayout(bg)
        bg_v.setSpacing(6)
        self.band_widgets = {}
        # Band status display (read-only labels updated on load)
        def _make_band_row(bname, colour):
            row = QHBoxLayout()
            lbl = QLabel(f"<b style='color:{colour}'>{bname}</b>")
            lbl.setFixedWidth(56)
            edit = QLineEdit()
            edit.setPlaceholderText("not loaded")
            edit.setReadOnly(True)
            row.addWidget(lbl)
            row.addWidget(edit)
            bg_v.addLayout(row)
            self.band_widgets[bname] = edit

        band_defs = [
            ("Red",    "#e74c3c"),
            ("Green",  "#27ae60"),
            ("NIR",    "#e67e22"),
            ("RedEdge","#f0a23a"),
            ("DEM",    "#5eb8f0"),
            ("Blue",   "#4a90d9"),
        ]
        for bname, colour in band_defs:
            _make_band_row(bname, colour)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        bg_v.addWidget(sep1)

        # Manual band addition
        manual_row = QHBoxLayout()
        self._manual_band_combo = QComboBox()
        self._manual_band_combo.addItems(["Red", "Green", "NIR", "RedEdge", "DEM", "Blue"])
        self._manual_band_combo.setFixedWidth(70)
        self._manual_path_edit = QLineEdit()
        self._manual_path_edit.setPlaceholderText("Select file...")
        manual_btn = QPushButton("Browse & Add")
        manual_btn.setFixedWidth(90)
        manual_btn.clicked.connect(self._add_band_manual)
        manual_row.addWidget(self._manual_band_combo)
        manual_row.addWidget(self._manual_path_edit)
        manual_row.addWidget(manual_btn)
        bg_v.addLayout(manual_row)

        upload_btn = QPushButton("Auto-detect from filename")
        upload_btn.clicked.connect(self._upload_bands)
        bg_v.addWidget(upload_btn)

        load_all_btn = QPushButton("Load all from folder")
        load_all_btn.clicked.connect(self._load_all_bands)
        bg_v.addWidget(load_all_btn)

        rgb_btn = QPushButton("Load RGB (3-band GeoTIFF)")
        rgb_btn.clicked.connect(self._load_rgb_tiff)
        bg_v.addWidget(rgb_btn)

        self.crs_label = QLabel("CRS: —")
        self.crs_label.setObjectName("coord")
        self.crs_label.setWordWrap(True)
        bg_v.addWidget(self.crs_label)

        self.crs_warn_label = QLabel("")
        self.crs_warn_label.setStyleSheet(f"color:{C_ERR};font-size:11px;")
        self.crs_warn_label.setWordWrap(True)
        bg_v.addWidget(self.crs_warn_label)
        lv.addWidget(bg)

        # AOI section
        ag = QGroupBox("Area of Interest (AOI)")
        ag_v = QVBoxLayout(ag)
        ag_v.setSpacing(6)

        tool_lbl = QLabel("Drawing tool:")
        tool_lbl.setObjectName("section")
        ag_v.addWidget(tool_lbl)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(4)
        self.tool_btns = {}
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for tid, icon, tip in [
            (MapCanvas.TOOL_RECT,  "Rect",   "Drag to draw a rectangle AOI"),
            (MapCanvas.TOOL_POLY,  "Poly",   "Click vertices, double-click or press Enter to close"),
            (MapCanvas.TOOL_FREE,  "Free",   "Freehand draw an AOI"),
            (MapCanvas.TOOL_PAN,   "Pan",    "Pan the map view"),
        ]:
            b = QPushButton(icon)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setFixedWidth(60)
            b.clicked.connect(lambda _, t=tid: self._set_tool(t))
            self._tool_group.addButton(b)
            tool_row.addWidget(b)
            self.tool_btns[tid] = b
        # default: Rect checked
        self.tool_btns[MapCanvas.TOOL_RECT].setChecked(True)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._clear_aoi)
        tool_row.addWidget(clear_btn)
        ag_v.addLayout(tool_row)

        coord_lbl = QLabel("Or enter coords (dataset CRS):")
        coord_lbl.setObjectName("section")
        ag_v.addWidget(coord_lbl)

        for attr, ph in [("_aoi_minx","Min X"), ("_aoi_miny","Min Y"),
                         ("_aoi_maxx","Max X"), ("_aoi_maxy","Max Y")]:
            rowc = QHBoxLayout()
            rowc.addWidget(QLabel(ph + ":"))
            e = QLineEdit()
            e.setPlaceholderText(ph)
            rowc.addWidget(e)
            setattr(self, attr, e)
            ag_v.addLayout(rowc)

        apply_coord_btn = QPushButton("Apply coordinates")
        apply_coord_btn.clicked.connect(self._apply_manual_coords)
        ag_v.addWidget(apply_coord_btn)

        shp_btn = QPushButton("Load Shapefile / GeoJSON AOI")
        shp_btn.clicked.connect(self._load_aoi_file)
        ag_v.addWidget(shp_btn)

        self.tile_count_lbl = QLabel("Tiles: —")
        self.tile_count_lbl.setObjectName("coord")
        ag_v.addWidget(self.tile_count_lbl)

        lv.addWidget(ag)

        # Tiling settings
        tg = QGroupBox("Tile settings")
        tg_grid = QGridLayout(tg)
        tg_grid.setSpacing(6)
        tg_grid.addWidget(QLabel("Tile size (m):"), 0, 0)
        self.tile_size_spin = QDoubleSpinBox()
        self.tile_size_spin.setRange(0.5, 10000.0)
        self.tile_size_spin.setValue(3.5)
        self.tile_size_spin.setSingleStep(0.5)
        tg_grid.addWidget(self.tile_size_spin, 0, 1)
        tg_grid.addWidget(QLabel("Edge handling:"), 1, 0)
        self.edge_combo = QComboBox()
        self.edge_combo.addItems(["discard", "zeros", "reflect"])
        tg_grid.addWidget(self.edge_combo, 1, 1)
        tg_grid.addWidget(QLabel("Tile naming:"), 2, 0)
        self.naming_combo = QComboBox()
        self.naming_combo.addItems(["rowcol", "geo coords", "timestamp"])
        tg_grid.addWidget(self.naming_combo, 2, 1)
        tg_grid.addWidget(QLabel("Output folder:"), 3, 0)
        out_row = QHBoxLayout()
        self.out_dir_edit = QLineEdit()
        self.out_dir_edit.setPlaceholderText("Choose folder…")
        out_row.addWidget(self.out_dir_edit)
        out_btn = QPushButton("Browse")
        out_btn.clicked.connect(self._choose_out_dir)
        out_row.addWidget(out_btn)
        tg_grid.addLayout(out_row, 3, 1)
        lv.addWidget(tg)

        # AOI Statistics
        stats_group = QGroupBox("AOI Statistics")
        stats_v = QVBoxLayout(stats_group)
        self.aoi_stats_widget = AOIStatsWidget()
        stats_v.addWidget(self.aoi_stats_widget)
        lv.addWidget(stats_group)

        # Export Tiles
        self.run_btn = QPushButton("Export Tiles")
        self.run_btn.setObjectName("primary")
        self.run_btn.setFixedHeight(38)
        self.run_btn.clicked.connect(self._run_tiling)
        lv.addWidget(self.run_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_tiling)
        lv.addWidget(self.cancel_btn)

        # Extract AOI
        extract_group = QGroupBox("Extract AOI")
        extract_v = QVBoxLayout(extract_group)
        extract_v.setSpacing(4)
        self.extract_btn = QPushButton("Extract AOI (single GeoTIFF)")
        self.extract_btn.setObjectName("primary")
        self.extract_btn.setFixedHeight(32)
        self.extract_btn.clicked.connect(self._run_extract)
        extract_v.addWidget(self.extract_btn)
        self.extract_cancel_btn = QPushButton("Cancel Extract")
        self.extract_cancel_btn.setObjectName("danger")
        self.extract_cancel_btn.setVisible(False)
        self.extract_cancel_btn.clicked.connect(self._cancel_extract)
        extract_v.addWidget(self.extract_cancel_btn)
        self.extract_pbar = QProgressBar()
        self.extract_pbar.setVisible(False)
        extract_v.addWidget(self.extract_pbar)
        self.extract_lbl = QLabel("")
        self.extract_lbl.setObjectName("coord")
        extract_v.addWidget(self.extract_lbl)
        lv.addWidget(extract_group)

        # Vigor Analysis button
        self.vigor_btn = QPushButton("Vigor Analysis & ML")
        self.vigor_btn.setObjectName("primary")
        self.vigor_btn.setFixedHeight(32)
        self.vigor_btn.clicked.connect(self._open_vigor_ml)
        lv.addWidget(self.vigor_btn)

        lv.addStretch()
        left_scroll.setWidget(left_w)

        # ── Right panel ──
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8)
        rv.setSpacing(6)

        self.tabs = QTabWidget()
        rv.addWidget(self.tabs)

        # Map tab
        map_tab = QWidget()
        map_v = QVBoxLayout(map_tab)
        map_v.setContentsMargins(0, 0, 0, 0)
        map_v.setSpacing(4)

        tb = QHBoxLayout()
        fit_btn = QPushButton("⊞ Fit view")
        fit_btn.clicked.connect(lambda: self.canvas.reset_view())
        tb.addWidget(fit_btn)
        self.band_selector = QComboBox()
        self.band_selector.setMinimumWidth(120)
        self.band_selector.setToolTip("Switch which loaded band is shown on canvas")
        self.band_selector.currentIndexChanged.connect(self._on_band_selected)
        tb.addWidget(self.band_selector)
        self.load_pbar = QProgressBar()
        self.load_pbar.setFixedWidth(120)
        self.load_pbar.setFixedHeight(14)
        self.load_pbar.setVisible(False)
        tb.addWidget(self.load_pbar)
        self.coord_lbl = QLabel("X: —   Y: —")
        self.coord_lbl.setObjectName("coord")
        tb.addWidget(self.coord_lbl)

        self.pixel_val_lbl = QLabel("Value: —")
        self.pixel_val_lbl.setStyleSheet(f"color:{C_WARN};font-size:11px;font-family:monospace;")
        tb.addWidget(self.pixel_val_lbl)

        tb.addStretch()

        self.show_aoi_cb = QCheckBox("AOI fill")
        self.show_aoi_cb.setChecked(False)
        self.show_aoi_cb.toggled.connect(lambda v: setattr(self.canvas, 'show_aoi_fill', v) or self.canvas.update())
        tb.addWidget(self.show_aoi_cb)
        self.show_tile_cb = QCheckBox("Tile fill")
        self.show_tile_cb.setChecked(True)
        self.show_tile_cb.toggled.connect(lambda v: setattr(self.canvas, 'show_tile_fill', v) or self.canvas.update())
        tb.addWidget(self.show_tile_cb)
        map_v.addLayout(tb)

        self.canvas = MapCanvas()
        self.canvas.aoi_changed.connect(self._on_aoi_changed)
        self.canvas.coord_moved.connect(self._on_coord_moved)
        self.canvas.pixel_inspected.connect(self._on_pixel_inspected)
        map_v.addWidget(self.canvas)
        self.tabs.addTab(map_tab, "Map view")

        # Histogram tab
        hist_tab = QWidget()
        hist_v = QVBoxLayout(hist_tab)
        hist_v.setContentsMargins(8, 8, 8, 8)
        self.histogram = HistogramWidget()
        hist_v.addWidget(self.histogram)
        self.hist_stats_lbl = QLabel("")
        self.hist_stats_lbl.setStyleSheet(f"color:{C_TEXT};font-size:11px;font-family:monospace;")
        self.hist_stats_lbl.setWordWrap(True)
        hist_v.addWidget(self.hist_stats_lbl)
        self.tabs.addTab(hist_tab, "Histogram")

        # Band Info tab
        info_tab = QWidget()
        info_v = QVBoxLayout(info_tab)
        info_v.setContentsMargins(8, 8, 8, 8)
        self.band_info = BandInfoWidget()
        info_v.addWidget(self.band_info)
        self.tabs.addTab(info_tab, "Band Info")

        # Log tab
        log_tab = QWidget()
        log_v = QVBoxLayout(log_tab)
        log_v.setContentsMargins(4, 4, 4, 4)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        log_v.addWidget(self.log)
        self.tabs.addTab(log_tab, "Log")

        # CSV Export tab (right-side) — parameters only
        csv_tab = QWidget()
        csv_v = QVBoxLayout(csv_tab)
        csv_v.setContentsMargins(8, 8, 8, 8)
        csv_v.setSpacing(6)

        csv_v.addWidget(QLabel("<b>Thresholds:</b>"))
        th_grid = QGridLayout()
        th_grid.addWidget(QLabel("GNDVI:"), 0, 0); self.csv_gndvi_th = QDoubleSpinBox(); self.csv_gndvi_th.setRange(0,1); self.csv_gndvi_th.setValue(0.4); self.csv_gndvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_gndvi_th, 0, 1)
        th_grid.addWidget(QLabel("GRVI:"), 1, 0);  self.csv_grvi_th = QDoubleSpinBox(); self.csv_grvi_th.setRange(0,1); self.csv_grvi_th.setValue(0.18); self.csv_grvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_grvi_th, 1, 1)
        th_grid.addWidget(QLabel("WDRVI:"), 2, 0); self.csv_wdrvi_th = QDoubleSpinBox(); self.csv_wdrvi_th.setRange(0,1); self.csv_wdrvi_th.setValue(0.6); self.csv_wdrvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_wdrvi_th, 2, 1)
        th_grid.addWidget(QLabel("DEM slope:"), 3, 0); self.csv_dem_slope = QDoubleSpinBox(); self.csv_dem_slope.setRange(-9999,9999); self.csv_dem_slope.setValue(67.637); th_grid.addWidget(self.csv_dem_slope, 3, 1)
        th_grid.addWidget(QLabel("DEM intercept:"), 4, 0); self.csv_dem_inter = QDoubleSpinBox(); self.csv_dem_inter.setRange(-9999,9999); self.csv_dem_inter.setValue(47.947); th_grid.addWidget(self.csv_dem_inter, 4, 1)
        csv_v.addLayout(th_grid)

        csv_v.addWidget(QLabel("<b>HSV vegetation mask:</b>"))
        hsv_g = QGroupBox()
        hsv_l = QGridLayout(hsv_g)
        hsv_l.addWidget(QLabel("H:"), 0, 0); self.csv_h_min = QSpinBox(); self.csv_h_min.setRange(0,179); self.csv_h_min.setValue(27); hsv_l.addWidget(self.csv_h_min, 0, 1)
        hsv_l.addWidget(QLabel("to"), 0, 2); self.csv_h_max = QSpinBox(); self.csv_h_max.setRange(0,179); self.csv_h_max.setValue(90); hsv_l.addWidget(self.csv_h_max, 0, 3)
        hsv_l.addWidget(QLabel("S:"), 1, 0); self.csv_s_min = QSpinBox(); self.csv_s_min.setRange(0,255); self.csv_s_min.setValue(27); hsv_l.addWidget(self.csv_s_min, 1, 1)
        hsv_l.addWidget(QLabel("to"), 1, 2); self.csv_s_max = QSpinBox(); self.csv_s_max.setRange(0,255); self.csv_s_max.setValue(255); hsv_l.addWidget(self.csv_s_max, 1, 3)
        hsv_l.addWidget(QLabel("V:"), 2, 0); self.csv_v_min = QSpinBox(); self.csv_v_min.setRange(0,255); self.csv_v_min.setValue(27); hsv_l.addWidget(self.csv_v_min, 2, 1)
        hsv_l.addWidget(QLabel("to"), 2, 2); self.csv_v_max = QSpinBox(); self.csv_v_max.setRange(0,255); self.csv_v_max.setValue(255); hsv_l.addWidget(self.csv_v_max, 2, 3)
        csv_v.addWidget(hsv_g)

        self.csv_result = QLabel("")
        csv_v.addWidget(self.csv_result)
        csv_v.addStretch()
        self.tabs.addTab(csv_tab, "CSV Export")

        # Progress strip at bottom of right panel
        prog_row = QHBoxLayout()
        self.csv_gen_btn = QPushButton("Next → Generate CSV")
        self.csv_gen_btn.setObjectName("primary")
        self.csv_gen_btn.setFixedHeight(28)
        self.csv_gen_btn.clicked.connect(self._csv_generate)
        prog_row.addWidget(self.csv_gen_btn)
        self.export_pbar = QProgressBar()
        self.export_pbar.setVisible(False)
        prog_row.addWidget(self.export_pbar)
        self.export_lbl = QLabel("")
        self.export_lbl.setObjectName("coord")
        prog_row.addWidget(self.export_lbl)
        rv.addLayout(prog_row)

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 840])
        main_h.addWidget(splitter)

    # ── Utility ──
    def status(self, msg):
        self.statusBar().showMessage(msg)

    def log_msg(self, msg, color=C_TEXT, switch_tab=False):
        self.log.append(f'<span style="color:{color}">{msg}</span>')
        if switch_tab:
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.log.parent()))

    # ── Band loading ──
    def _assign_band_index(self, bname, path):
        """Auto-assign band index: if same file is used by multiple bands, increment index."""
        existing = [k for k, v in self.band_paths.items() if v == path and k != bname]
        self.band_indices[bname] = len(existing) + 1

    def _add_band_manual(self):
        bname = self._manual_band_combo.currentText()
        path, _ = QFileDialog.getOpenFileName(self, f"Select {bname} band GeoTIFF",
            "", "GeoTIFF (*.tif *.tiff);;All files (*)")
        if not path:
            return
        self.band_paths[bname] = path
        self._assign_band_index(bname, path)
        self._manual_path_edit.setText(path)
        if bname in self.band_widgets:
            idx = self.band_indices.get(bname, 1)
            lbl = f"{Path(path).name}" if idx == 1 else f"{Path(path).name} (b{idx})"
            self.band_widgets[bname].setText(lbl)
            self.band_widgets[bname].setToolTip(path)
        self.log_msg(f"Loaded {bname}: {path}  (band index={self.band_indices.get(bname, 1)})", C_ACCENT2)
        self._validate_crs()
        self._populate_band_selector()
        self._load_preview(path)

    def _upload_bands(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a band GeoTIFF",
            "", "GeoTIFF (*.tif *.tiff);;All files (*)")
        if not path:
            return
        fname = Path(path).name
        detected = auto_detect_band(fname)
        if not detected:
            self.log_msg(f"Could not detect band type: {fname}", C_WARN)
            return
        if detected in self.band_paths:
            self.log_msg(f"{detected} already loaded", C_WARN)
            return
        self.band_paths[detected] = path
        self._assign_band_index(detected, path)
        if detected in self.band_widgets:
            idx = self.band_indices.get(detected, 1)
            lbl = f"{fname}" if idx == 1 else f"{fname} (b{idx})"
            self.band_widgets[detected].setText(lbl)
            self.band_widgets[detected].setToolTip(path)
        self.log_msg(f"Loaded {detected}: {fname}  (band index={self.band_indices.get(detected, 1)})", C_ACCENT2)
        self._validate_crs()
        self._populate_band_selector()
        self._load_preview(path)

    def _load_all_bands(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder with band GeoTIFFs")
        if not folder:
            return
        found = 0
        # First pass: detect files, check band counts
        file_info = []
        for fname in sorted(os.listdir(folder)):
            low = fname.lower()
            if not (low.endswith(".tif") or low.endswith(".tiff")):
                continue
            fpath = os.path.join(folder, fname)
            detected = auto_detect_band(fname)
            if detected:
                file_info.append((fname, fpath, detected))
        # Second pass: for multi-band files, assign sequential indices
        for fname, fpath, detected in file_info:
            if detected in self.band_paths:
                self.log_msg(f"{detected} already loaded (from {Path(self.band_paths[detected]).name})", C_WARN)
                continue
            self.band_paths[detected] = fpath
            self._assign_band_index(detected, fpath)
            idx = self.band_indices.get(detected, 1)
            if detected in self.band_widgets:
                lbl = f"{fname}" if idx == 1 else f"{fname} (b{idx})"
                self.band_widgets[detected].setText(lbl)
                self.band_widgets[detected].setToolTip(fpath)
            found += 1
            self.log_msg(f"Auto-detected {detected}: {fname}  (band index={idx})", C_ACCENT2)
        if found == 0:
            self.log_msg("No matching band files found in folder", C_WARN)
            return
        self._validate_crs()
        self._populate_band_selector()
        for bname in ["NIR", "Red", "Green", "DEM", "Blue", "RedEdge"]:
            if bname in self.band_paths:
                self._load_preview(self.band_paths[bname])
                break

    def _load_rgb_tiff(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select 3-band RGB GeoTIFF",
            "", "GeoTIFF (*.tif *.tiff);;All files (*)")
        if not path:
            return
        try:
            with rasterio.open(path) as ds:
                if ds.count < 3:
                    QMessageBox.warning(self, "Invalid bands",
                        f"File has only {ds.count} band(s). Expected at least 3.")
                    return
                for idx, bname in [(1, "Red"), (2, "Green"), (3, "Blue")]:
                    self.band_paths[bname] = path
                    self.band_indices[bname] = idx
                    if bname in self.band_widgets:
                        self.band_widgets[bname].setText(f"{Path(path).name} (b{idx})")
                        self.band_widgets[bname].setToolTip(path)
                    self.log_msg(f"Loaded {bname} (band {idx}): {path}", C_ACCENT2)
                self._validate_crs()
                self._populate_band_selector()
                self.load_pbar.setVisible(True)
                self.load_pbar.setValue(20)
                QApplication.processEvents()
                w, h, crs = ds.width, ds.height, ds.crs
                left, bottom, right, top = ds.bounds
                max_dim = 1024
                scale = min(max_dim / w, max_dim / h, 1.0)
                ow = max(int(w * scale), 1)
                oh = max(int(h * scale), 1)
                r = ds.read(1, out_shape=(oh, ow), resampling=Resampling.average)
                g = ds.read(2, out_shape=(oh, ow), resampling=Resampling.average)
                b = ds.read(3, out_shape=(oh, ow), resampling=Resampling.average)
                self.load_pbar.setValue(60)
                def _norm(band):
                    v = band.astype(float)
                    lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
                    if hi == lo:
                        return np.zeros_like(v, dtype=np.uint8)
                    return np.clip((v - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
                rn, gn, bn = _norm(r), _norm(g), _norm(b)
                rgba = np.stack([rn, gn, bn, np.full((oh, ow), 255, dtype=np.uint8)], axis=-1)
                img = QImage(rgba.tobytes(), ow, oh, ow * 4, QImage.Format.Format_RGBA8888)
                pix = QPixmap.fromImage(img)
                self.load_pbar.setValue(100)
                self.canvas.load_overview(pix, (left, bottom, right, top), crs)
                self.load_pbar.setVisible(False)
                self._raster_crs = crs
                self._raster_bounds = (left, bottom, right, top)
                epsg = crs.to_epsg() if crs else None
                self.crs_label.setText(f"CRS: EPSG:{epsg}" if epsg else f"CRS: {crs}"[:40])
                self.status(f"Loaded RGB -- {w}x{h}  {self.crs_label.text()}")
        except Exception as e:
            self.load_pbar.setVisible(False)
            QMessageBox.critical(self, "Error", f"Failed to load RGB file:\n{e}")

    def _load_preview(self, path, band_index=1):
        if self._loader and self._loader.isRunning():
            self._loader.quit()
            self._loader.wait(1000)
        self.load_pbar.setVisible(True)
        self.load_pbar.setValue(0)
        self._loader = BandLoader(path, band_index)
        self._loader.done.connect(self._on_band_loaded)
        self._loader.error.connect(lambda e: (
            self.log_msg(f"Preview error: {e[:200]}", C_ERR),
            self.load_pbar.setVisible(False)))
        self._loader.progress.connect(self.load_pbar.setValue)
        self._loader.start()

    def _validate_crs(self):
        warnings = validate_band_crs(self.band_paths)
        if warnings:
            self.crs_warn_label.setText("⚠ " + "\n".join(warnings)[:200])
        else:
            self.crs_warn_label.setText("")

    def _populate_band_selector(self):
        current = self.band_selector.currentText()
        self.band_selector.blockSignals(True)
        self.band_selector.clear()
        has_rgb = all(b in self.band_paths for b in ("Red", "Green", "Blue"))
        if has_rgb:
            self.band_selector.addItem("RGB")
        order = ["Red", "Green", "Blue", "NIR", "RedEdge", "DEM"]
        for b in order:
            if b in self.band_paths:
                self.band_selector.addItem(b)
        self.band_selector.blockSignals(False)
        if self.band_selector.count() > 0:
            idx = self.band_selector.findText(current)
            self.band_selector.setCurrentIndex(max(0, idx))

    def _on_band_selected(self, idx):
        if idx < 0:
            return
        name = self.band_selector.currentText()
        if name == "RGB":
            self._load_rgb_preview()
        elif name in self.band_paths:
            self._load_preview(self.band_paths[name], self.band_indices.get(name, 1))

    def _load_rgb_preview(self):
        rpath = self.band_paths.get("Red")
        gpath = self.band_paths.get("Green")
        bpath = self.band_paths.get("Blue")
        if not rpath or not gpath or not bpath:
            return
        try:
            with rasterio.open(rpath) as ds:
                w, h, crs = ds.width, ds.height, ds.crs
                left, bottom, right, top = ds.bounds
                max_dim = 1024
                scale = min(max_dim / w, max_dim / h, 1.0)
                ow = max(int(w * scale), 1)
                oh = max(int(h * scale), 1)
                r = ds.read(self.band_indices.get("Red", 1), out_shape=(oh, ow), resampling=Resampling.average)
            with rasterio.open(gpath) as ds:
                g = ds.read(self.band_indices.get("Green", 1), out_shape=(oh, ow), resampling=Resampling.average)
            with rasterio.open(bpath) as ds:
                b = ds.read(self.band_indices.get("Blue", 1), out_shape=(oh, ow), resampling=Resampling.average)
            def _norm(band):
                v = band.astype(float)
                lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
                if hi == lo:
                    return np.zeros_like(v, dtype=np.uint8)
                return np.clip((v - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
            rn, gn, bn = _norm(r), _norm(g), _norm(b)
            rgba = np.stack([rn, gn, bn, np.full((oh, ow), 255, dtype=np.uint8)], axis=-1)
            img = QImage(rgba.tobytes(), ow, oh, ow * 4, QImage.Format.Format_RGBA8888)
            pix = QPixmap.fromImage(img)
            self.canvas.load_overview(pix, (left, bottom, right, top), crs)
        except Exception as e:
            self.log_msg(f"RGB preview error: {e}", C_ERR)

    def _on_band_loaded(self, path, data, bounds, orig_w, orig_h, pw, ph):
        try:
            self.raster_bounds = (bounds.left, bounds.bottom, bounds.right, bounds.top) if hasattr(bounds, 'left') else bounds
            self._raster_bounds = self.raster_bounds
            self._raster_crs = self._get_crs(path)
            self._dataset_width = orig_w
            self._dataset_height = orig_h
            lo, hi = np.nanpercentile(data, [2, 98])
            if lo == hi:
                hi = lo + 1
            normed = np.clip((data - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
            normed[np.isnan(data)] = 0
            rgba = np.zeros((ph, pw, 4), dtype=np.uint8)
            rgba[..., 0] = normed
            rgba[..., 1] = (normed * 0.85).astype(np.uint8)
            rgba[..., 2] = (normed * 0.65).astype(np.uint8)
            rgba[..., 3] = 255
            img = QImage(rgba.tobytes(), pw, ph, pw * 4, QImage.Format.Format_RGBA8888)
            pix = QPixmap.fromImage(img)
            self.canvas.load_overview(pix, self.raster_bounds, self._raster_crs)
            self.load_pbar.setVisible(False)
            epsg = self._raster_crs.to_epsg() if self._raster_crs else None
            crs_str = f"EPSG:{epsg}" if epsg else str(self._raster_crs)[:40]
            self.crs_label.setText(f"CRS: {crs_str}")
            self.log_msg(f"Preview: {Path(path).name}  |  {orig_w}x{orig_h} px  |  CRS: {crs_str}", C_ACCENT2)
        except Exception as e:
            self.log_msg(f"Preview error: {e}", C_ERR)
            self.load_pbar.setVisible(False)

    def _get_crs(self, path):
        try:
            with rasterio.open(path) as ds:
                return ds.crs
        except Exception:
            return None

    def _set_tool(self, tid):
        self.canvas.tool = tid
        self.canvas.setFocus()
        # reset incomplete drawings
        self.canvas._rect_start = None
        self.canvas._rect_cur = None
        self.canvas._poly_pts = []
        self.canvas._free_pts = []
        self.canvas._dragging_free = False
        self.canvas.update()

    # ── AOI events ──
    def _on_aoi_changed(self, aoi_geo):
        if aoi_geo is None:
            self.canvas.tile_geo = []
            self.canvas.tile_polys_screen = []
            self.canvas.update()
            return
        if self._raster_crs is None:
            return
        try:
            tile_m = self.tile_size_spin.value()
            polys = compute_tile_polys(aoi_geo, tile_m, self._raster_crs,
                                        band_paths=self.band_paths, band_indices=self.band_indices)
            stats = compute_aoi_stats(aoi_geo, tile_m, self._raster_crs,
                                       band_paths=self.band_paths, band_indices=self.band_indices)
            self.canvas.set_tile_grid(polys)
            self.aoi_stats_widget.update_stats(stats)
            self.tile_count_lbl.setText(
                f"Tiles: {len(polys)}  ({tile_m:.0f} m)")
        except Exception as e:
            self.tile_count_lbl.setText(f"Stats error: {e}")

    def _clear_aoi(self):
        self.canvas.aoi_screen = []
        self.canvas.aoi_geo = None
        self.canvas.clear_tiles()
        for e in [self._aoi_minx, self._aoi_miny, self._aoi_maxx, self._aoi_maxy]:
            e.clear()
        self.tile_count_lbl.setText("Tiles: —")
        self.canvas.update()

    def _apply_manual_coords(self):
        try:
            minx = float(self._aoi_minx.text())
            miny = float(self._aoi_miny.text())
            maxx = float(self._aoi_maxx.text())
            maxy = float(self._aoi_maxy.text())
        except ValueError:
            QMessageBox.warning(self, "Invalid", "Enter numeric coordinates.")
            return
        poly = box(minx, miny, maxx, maxy)
        if self._raster_crs:
            tl = self.canvas._geo_to_screen(minx, maxy)
            tr = self.canvas._geo_to_screen(maxx, maxy)
            br = self.canvas._geo_to_screen(maxx, miny)
            bl = self.canvas._geo_to_screen(minx, miny)
            self.canvas.aoi_screen = [tl, tr, br, bl]
            self.canvas.aoi_geo    = poly
            self.canvas.update()
        self._on_aoi_changed(poly)

    def _load_aoi_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load AOI",
            "", "Shapefile (*.shp);;GeoJSON (*.geojson *.json);;All (*)")
        if not path:
            return
        try:
            if path.lower().endswith(".shp"):
                import shapefile
                reader = shapefile.Reader(path)
                from shapely.geometry import Polygon as SPolygon
                polys = []
                for s in reader.shapes():
                    pts = list(s.points)
                    ring_starts = list(s.parts) + [len(pts)]
                    outer = pts[ring_starts[0]:ring_starts[1]]
                    holes = [pts[ring_starts[i]:ring_starts[i+1]] for i in range(1, len(ring_starts)-1)]
                    polys.append(SPolygon(outer, holes))
                from shapely.ops import unary_union
                poly = unary_union(polys)
            else:
                import json
                with open(path) as f:
                    gj = json.load(f)
                from shapely.geometry import shape as shp_shape
                feats = gj.get("features", [])
                if not feats:
                    poly = shp_shape(gj)
                else:
                    from shapely.ops import unary_union
                    geoms = [shp_shape(f["geometry"]) for f in feats]
                    poly = unary_union(geoms)
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda p: p.area)
            b = poly.bounds
            self._aoi_minx.setText(f"{b[0]:.6f}")
            self._aoi_miny.setText(f"{b[1]:.6f}")
            self._aoi_maxx.setText(f"{b[2]:.6f}")
            self._aoi_maxy.setText(f"{b[3]:.6f}")
            pts = [self.canvas._geo_to_screen(x, y) for x, y in poly.exterior.coords]
            self.canvas.aoi_screen = pts
            self.canvas.aoi_geo = poly
            self.canvas.update()
            self._on_aoi_changed(poly)
            self.log_msg(f"AOI loaded from: {Path(path).name}", C_ACCENT2)
        except Exception as ex:
            self.log_msg(f"AOI load error: {ex}", C_ERR)
            QMessageBox.warning(self, "AOI Error", f"Failed to load AOI:\n{ex}")

    def _on_coord_moved(self, lon, lat):
        self.coord_lbl.setText(f"X: {lon:.6f}  Y: {lat:.6f}")

    def _on_pixel_inspected(self, lon, lat):
        if lon is None:
            return
        left, bottom, right, top = self._raster_bounds
        if not self.band_paths:
            return
        bname = next(iter(self.band_paths))
        try:
            with rasterio.open(self.band_paths[bname]) as ds:
                h, w = ds.height, ds.width
                col = int((lon - left) / (right - left) * w)
                row = int((1 - (lat - bottom) / (top - bottom)) * h)
                if 0 <= col < w and 0 <= row < h:
                    val = ds.read(self.band_indices.get(bname, 1), window=Window(col, row, 1, 1))[0, 0]
                    self.pixel_val_lbl.setText(f"Value: {val:.2f}")
        except Exception:
            pass

    # ── Output folder ──
    def _choose_out_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.out_dir_edit.setText(folder)

    # ── Tiling ──
    def _run_tiling(self):
        if not self.band_paths:
            QMessageBox.warning(self, "No bands", "Load at least one band first.")
            return
        if self.canvas.aoi_geo is None:
            QMessageBox.warning(self, "No AOI", "Define an Area of Interest first.")
            return
        out_dir = self.out_dir_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "No output", "Choose an output folder.")
            return

        edge_map = {0: "discard", 1: "zeros", 2: "reflect"}
        edge_handling = edge_map[self.edge_combo.currentIndex()]

        naming_map = {0: "rowcol", 1: "geo", 2: "timestamp"}
        tile_naming = naming_map[self.naming_combo.currentIndex()]

        layers = ["RGB", "GNDVI", "GRVI", "WDRVI", "DEM"]

        self.run_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.export_pbar.setVisible(True)
        self.export_pbar.setValue(0)
        self.export_lbl.setText("Starting…")
        self.log_msg("--- Export started ---", C_ACCENT)
        for bname, bpath in self.band_paths.items():
            idx = self.band_indices.get(bname, 1)
            self.log_msg(f"  band [{bname}] = {Path(bpath).name} (index {idx})", C_ACCENT2)

        self._worker = TilingWorker(
            band_paths     = self.band_paths,
            aoi_geo        = self.canvas.aoi_geo,
            tile_m         = self.tile_size_spin.value(),
            output_dir     = out_dir,
            export_layers  = layers,
            overlap        = 0.0,
            edge_handling  = edge_handling,
            band_indices     = self.band_indices,
            tile_naming    = tile_naming,
        )
        self._worker.progress.connect(self._on_tile_progress)
        self._worker.tile_done.connect(lambda p: self.log_msg(f"  + {Path(p).name}", C_ACCENT2))
        self._worker.finished.connect(self._on_tiling_done)
        self._worker.error.connect(self._on_tiling_error)
        def _clean_worker(): w = self._worker; self._worker = None; w.deleteLater()
        self._worker.finished.connect(_clean_worker)
        self._worker.start()

    def _on_tile_progress(self, cur, total, msg):
        pct = int(cur / max(total, 1) * 100)
        self.export_pbar.setValue(pct)
        self.export_lbl.setText(msg)
        self.status(msg)

    def _on_tiling_done(self, n, out_dir):
        self.export_pbar.setValue(100)
        self.export_lbl.setText(f"Done — {n} tiles written")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.log_msg(f"Done -- exported {n} tiles to {out_dir}", C_ACCENT2, switch_tab=True)
        self.log_msg(f"   tile_index.geojson written", C_MUTED)
        self.status(f"Done — {n} tiles in {out_dir}")
        QMessageBox.information(self, "Export complete",
            f"Exported {n} tiles to:\n{out_dir}\n\nA tile_index.geojson was also written.")

    def _on_tiling_error(self, err):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.export_pbar.setVisible(False)
        self.log_msg("Export error:\n" + err, C_ERR, switch_tab=True)
        self.status("Export failed — see log")
        QMessageBox.critical(self, "Export error", err[:500])

    def _cancel_tiling(self):
        if self._worker:
            self._worker.cancel()
        self.cancel_btn.setVisible(False)
        self.run_btn.setEnabled(True)
        self.export_pbar.setVisible(False)
        self.log_msg("Export cancelled by user", C_WARN, switch_tab=True)
        self.status("Export cancelled")

    # ── Extract AOI ──
    def _run_extract(self):
        if not self.band_paths:
            QMessageBox.warning(self, "No bands", "Load at least one band first.")
            return
        if self.canvas.aoi_geo is None:
            QMessageBox.warning(self, "No AOI", "Define an Area of Interest first.")
            return
        out_dir = self.out_dir_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "No output", "Choose an output folder.")
            return

        self.extract_btn.setEnabled(False)
        self.extract_cancel_btn.setVisible(True)
        self.extract_pbar.setVisible(True)
        self.extract_pbar.setValue(0)
        self.extract_lbl.setText("Starting…")
        self.log_msg("--- AOI extract started ---", C_ACCENT)

        self._extract_worker = ExtractWorker(
            band_paths  = self.band_paths,
            aoi_geo     = self.canvas.aoi_geo,
            output_dir  = out_dir,
        )
        self._extract_worker.progress.connect(self._on_extract_progress)
        self._extract_worker.done.connect(self._on_extract_done)
        self._extract_worker.error.connect(self._on_extract_error)
        def _clean_extract(): w = self._extract_worker; self._extract_worker = None; w.deleteLater()
        self._extract_worker.finished.connect(_clean_extract)
        self._extract_worker.start()

    def _on_extract_progress(self, pct, msg):
        self.extract_pbar.setValue(pct)
        self.extract_lbl.setText(msg)
        self.status(msg)

    def _on_extract_done(self, out_dir):
        self.extract_pbar.setValue(100)
        self.extract_lbl.setText("Done")
        self.extract_btn.setEnabled(True)
        self.extract_cancel_btn.setVisible(False)
        self.log_msg(f"AOI extracted — files written to {out_dir}", C_ACCENT2, switch_tab=True)
        self.status(f"Extract done — {out_dir}")
        QMessageBox.information(self, "Extract complete",
            f"AOI extracted to:\n{out_dir}")

    def _on_extract_error(self, err):
        self.extract_btn.setEnabled(True)
        self.extract_cancel_btn.setVisible(False)
        self.extract_pbar.setVisible(False)
        self.log_msg("Extract error:\n" + err, C_ERR, switch_tab=True)
        self.status("Extract failed — see log")
        QMessageBox.critical(self, "Extract error", err[:500])

    def _cancel_extract(self):
        if self._extract_worker:
            self._extract_worker.cancel()
        self.extract_cancel_btn.setVisible(False)
        self.extract_btn.setEnabled(True)
        self.extract_pbar.setVisible(False)
        self.log_msg("Extract cancelled by user", C_WARN, switch_tab=True)
        self.status("Extract cancelled")

    def _open_vigor_ml(self):
        self._vigor_ml_window = VigorMLWindow(self)
        self._vigor_ml_window.show()

    # ── CSV Export (right-side tab) ─────────────────────────────────────────
    def _csv_generate(self):
        base = Path(self.out_dir_edit.text().strip())
        if not base.exists():
            QMessageBox.warning(self, "No export", "Export tiles first. No output folder found.")
            return
        self.csv_gen_btn.setEnabled(False)

        def tile_name(path):
            return Path(path).stem

        # Collect all tile names from every subfolder
        all_tile_names = set()
        sub_dirs = [
            base / "DEM", base / "dem", base / "Dem",
            base / "GNDVI", base / "gndvi",
            base / "GRVI", base / "grvi",
            base / "WDRVI", base / "wdrvi", base / "WRDVI", base / "wrdvi",
            base / "Optical", base / "optical", base / "RGB", base / "rgb",
        ]
        found_dirs = []
        for sd in sub_dirs:
            if sd.exists():
                found_dirs.append(str(sd.relative_to(base)))
                for p in sd.glob("*.tif"):
                    all_tile_names.add(tile_name(p))
        if not all_tile_names:
            self.csv_result.setText(f"No TIF files found. Checked: {', '.join(found_dirs) if found_dirs else 'no matching subfolders'}.")
            self.csv_gen_btn.setEnabled(True)
            return
        all_tile_names = sorted(all_tile_names)
        self.csv_result.setText(f"Found {len(all_tile_names)} tiles in: {', '.join(found_dirs)}")
        QApplication.processEvents()

        # ── DEM → Height ──
        self.csv_result.setText("Processing DEM…")
        QApplication.processEvents()
        height_dict = {}
        for d in [base / "DEM", base / "dem", base / "Dem"]:
            if d.exists():
                for p in d.glob("*.tif"):
                    try:
                        img = np.array(Image.open(p)).astype(float)
                        cal = self.csv_dem_slope.value() * img.mean() + self.csv_dem_inter.value()
                        height_dict[tile_name(p)] = cal
                    except Exception:
                        continue
                break

        # ── Indices: GNDVI, GRVI, WDRVI ──
        self.csv_result.setText("Processing indices…")
        QApplication.processEvents()
        vi_dir_map = {"GNDVI": [base / "GNDVI", base / "gndvi"],
                      "GRVI":  [base / "GRVI",  base / "grvi"],
                      "WDRVI": [base / "WDRVI", base / "wdrvi", base / "WRDVI", base / "wrdvi"]}
        vi_thresh = {"GNDVI": self.csv_gndvi_th.value(),
                     "GRVI":  self.csv_grvi_th.value(),
                     "WDRVI": self.csv_wdrvi_th.value()}
        vi_dicts = {k: {} for k in vi_dir_map}
        for label, dirs in vi_dir_map.items():
            for d in dirs:
                if d.exists():
                    for p in d.glob("*.tif"):
                        try:
                            raw = np.array(Image.open(p)).astype(float)
                            vi = (raw / 65535.0) * 2.0 - 1.0  # uint16 [0,65535] → float [-1,1]
                            vals = vi[vi > vi_thresh[label]]
                            vi_dicts[label][tile_name(p)] = vals.mean() if vals.size > 0 else ""
                        except Exception:
                            continue
                    break

        # ── Optical → F Cover ──
        self.csv_result.setText("Processing Optical…")
        QApplication.processEvents()
        fcover_dict = {}
        for d in [base / "Optical", base / "optical", base / "RGB", base / "rgb"]:
            if d.exists():
                for p in d.glob("*.tif"):
                    try:
                        img = cv2.imread(str(p))
                        if img is None:
                            continue
                        total_pixels = img.size
                        black_pixels = np.sum(img == 0)
                        non_zero_pixels = total_pixels - black_pixels
                        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                        mask = cv2.inRange(hsv,
                            (self.csv_h_min.value(), self.csv_s_min.value(), self.csv_v_min.value()),
                            (self.csv_h_max.value(), self.csv_s_max.value(), self.csv_v_max.value()))
                        mask = cv2.medianBlur(mask, 3)
                        veg_pixels = cv2.countNonZero(mask)
                        fcover_dict[tile_name(p)] = (veg_pixels / (non_zero_pixels / 3)) * 100 if non_zero_pixels else 0
                    except Exception:
                        continue
                break

        rows = []
        for name in all_tile_names:
            rows.append({
                "Tile name": name,
                "GNDVI": vi_dicts["GNDVI"].get(name, ""),
                "WDRVI": vi_dicts["WDRVI"].get(name, ""),
                "GRVI":  vi_dicts["GRVI"].get(name, ""),
                "F Cover": fcover_dict.get(name, ""),
                "Height": height_dict.get(name, ""),
            })
        df = pd.DataFrame(rows)
        out_csv = str(base / "Next.csv")
        df.to_csv(out_csv, index=False)
        self.csv_result.setText(f"✅ Generated: Next.csv ({len(rows)} tiles)")
        self.csv_gen_btn.setEnabled(True)

    # ── Cleanup ──
    def closeEvent(self, event):
        if self._loader and self._loader.isRunning():
            self._loader.quit()
            self._loader.wait(1000)
        try:
            if self._worker is not None and self._worker.isRunning():
                self._worker.cancel()
                self._worker.quit()
                self._worker.wait(2000)
        except RuntimeError:
            pass
        try:
            if self._extract_worker is not None and self._extract_worker.isRunning():
                self._extract_worker.cancel()
                self._extract_worker.quit()
                self._extract_worker.wait(2000)
        except RuntimeError:
            pass
        event.accept()


# ══════════════════════════════════════════════════════════════════════════════
# Extract Worker
# ══════════════════════════════════════════════════════════════════════════════

class ExtractWorker(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, band_paths=None, aoi_geo=None, output_dir=""):
        super().__init__()
        self.band_paths = band_paths or {}
        self.aoi_geo = aoi_geo
        self.output_dir = output_dir
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            out = Path(self.output_dir)
            out.mkdir(parents=True, exist_ok=True)
            for idx, (bname, bpath) in enumerate(self.band_paths.items()):
                if self._cancelled:
                    return
                self.progress.emit(int((idx + 1) / len(self.band_paths) * 100), f"Clipping {bname}…")
                with rasterio.open(bpath) as src:
                    out_img, out_transform = rasterio_mask(src, [self.aoi_geo], crop=True, nodata=src.nodata)
                    profile = src.profile.copy()
                    profile.update({
                        "height": out_img.shape[1],
                        "width": out_img.shape[2],
                        "transform": out_transform,
                        "compress": "lzw",
                    })
                    dst_path = out / f"{bname}_aoi.tif"
                    with rasterio.open(str(dst_path), "w", **profile) as dst:
                        dst.write(out_img)
            self.done.emit(str(out))
        except Exception as e:
            self.error.emit(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# VigorML Window
# ══════════════════════════════════════════════════════════════════════════════

class VigorMLWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vigor Analysis & ML Training")
        self.setMinimumSize(900, 700)
        self._export_dir = None
        self._dtm_path = None
        self._data_cache = {}
        self._model = None
        self.setup_ui()

    def setup_ui(self):
        v = QVBoxLayout(self)
        v.setSpacing(6)

        # Load folder
        load_row = QHBoxLayout()
        self.load_btn = QPushButton("Load Export Folder")
        self.load_btn.clicked.connect(self._load_folder)
        load_row.addWidget(self.load_btn)
        self.folder_lbl = QLabel("No folder loaded")
        self.folder_lbl.setStyleSheet("color:#888;")
        load_row.addWidget(self.folder_lbl, 1)
        v.addLayout(load_row)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs)

        # Canopy Cover tab
        cc_tab = QWidget()
        cc_v = QVBoxLayout(cc_tab)
        th_row = QHBoxLayout()
        th_row.addWidget(QLabel("GNDVI threshold:"))
        self.cc_thresh = QDoubleSpinBox()
        self.cc_thresh.setRange(0.0, 1.0)
        self.cc_thresh.setSingleStep(0.05)
        self.cc_thresh.setValue(0.3)
        th_row.addWidget(self.cc_thresh)
        self.cc_btn = QPushButton("Compute")
        self.cc_btn.clicked.connect(self._compute_cc)
        th_row.addWidget(self.cc_btn)
        cc_v.addLayout(th_row)
        self.cc_result = QLabel("—")
        cc_v.addWidget(self.cc_result)
        self.cc_progress = QProgressBar()
        self.cc_progress.setVisible(False)
        cc_v.addWidget(self.cc_progress)
        cc_v.addStretch()
        self.tabs.addTab(cc_tab, "Canopy Cover")

        # Crop Height tab
        ch_tab = QWidget()
        ch_v = QVBoxLayout(ch_tab)
        dtm_row = QHBoxLayout()
        dtm_btn = QPushButton("Load DTM")
        dtm_btn.clicked.connect(self._load_dtm)
        dtm_row.addWidget(dtm_btn)
        ch_v.addLayout(dtm_row)
        self.ch_result = QLabel("—")
        ch_v.addWidget(self.ch_result)
        ch_v.addStretch()
        self.tabs.addTab(ch_tab, "Crop Height")

        # Vigor Classification tab
        vc_tab = QWidget()
        vc_v = QVBoxLayout(vc_tab)
        vc_v.addWidget(QLabel("Vigor classification based on GNDVI thresholds."))
        self.vc_result = QLabel("—")
        vc_v.addWidget(self.vc_result)
        vc_v.addStretch()
        self.tabs.addTab(vc_tab, "Vigor Classification")

        # ML Training tab
        ml_tab = QWidget()
        ml_v = QVBoxLayout(ml_tab)
        ml_v.addWidget(QLabel("Write a Python snippet to train a model."))
        self.code_edit = QPlainTextEdit()
        self.code_edit.setPlainText(
            "from sklearn.ensemble import RandomForestRegressor\n"
            "model = RandomForestRegressor(n_estimators=100)\n"
            "model.fit(X, y)\n"
            "self._model = model\n"
        )
        ml_v.addWidget(self.code_edit)
        train_row = QHBoxLayout()
        self.train_btn = QPushButton("Train")
        self.train_btn.clicked.connect(self._train_model)
        train_row.addWidget(self.train_btn)
        self.ml_result = QLabel("")
        train_row.addWidget(self.ml_result, 1)
        ml_v.addLayout(train_row)
        ml_v.addStretch()
        self.tabs.addTab(ml_tab, "ML Training")

        # CSV Export tab
        csv_tab = QWidget()
        csv_v = QVBoxLayout(csv_tab)
        csv_v.setContentsMargins(8, 8, 8, 8)
        csv_v.setSpacing(6)

        csv_sel = QHBoxLayout()
        self.csv_folder_btn = QPushButton("Select Data Folder")
        self.csv_folder_btn.clicked.connect(self._csv_select_folder)
        csv_sel.addWidget(self.csv_folder_btn)
        self.csv_folder_lbl = QLabel("Not selected")
        self.csv_folder_lbl.setStyleSheet("color:#888;")
        csv_sel.addWidget(self.csv_folder_lbl, 1)
        csv_v.addLayout(csv_sel)

        csv_v.addWidget(QLabel("<b>Thresholds:</b>"))
        th_grid = QGridLayout()
        th_grid.addWidget(QLabel("GNDVI:"), 0, 0); self.csv_gndvi_th = QDoubleSpinBox(); self.csv_gndvi_th.setRange(0,1); self.csv_gndvi_th.setValue(0.4); self.csv_gndvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_gndvi_th, 0, 1)
        th_grid.addWidget(QLabel("GRVI:"), 1, 0);  self.csv_grvi_th = QDoubleSpinBox(); self.csv_grvi_th.setRange(0,1); self.csv_grvi_th.setValue(0.18); self.csv_grvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_grvi_th, 1, 1)
        th_grid.addWidget(QLabel("WDRVI:"), 2, 0); self.csv_wdrvi_th = QDoubleSpinBox(); self.csv_wdrvi_th.setRange(0,1); self.csv_wdrvi_th.setValue(0.6); self.csv_wdrvi_th.setSingleStep(0.05); th_grid.addWidget(self.csv_wdrvi_th, 2, 1)
        th_grid.addWidget(QLabel("DEM slope:"), 3, 0); self.csv_dem_slope = QDoubleSpinBox(); self.csv_dem_slope.setRange(-9999,9999); self.csv_dem_slope.setValue(67.637); th_grid.addWidget(self.csv_dem_slope, 3, 1)
        th_grid.addWidget(QLabel("DEM intercept:"), 4, 0); self.csv_dem_inter = QDoubleSpinBox(); self.csv_dem_inter.setRange(-9999,9999); self.csv_dem_inter.setValue(47.947); th_grid.addWidget(self.csv_dem_inter, 4, 1)
        csv_v.addLayout(th_grid)

        csv_v.addWidget(QLabel("<b>HSV vegetation mask:</b>"))
        hsv_g = QGroupBox()
        hsv_l = QGridLayout(hsv_g)
        hsv_l.addWidget(QLabel("H:"), 0, 0); self.csv_h_min = QSpinBox(); self.csv_h_min.setRange(0,179); self.csv_h_min.setValue(27); hsv_l.addWidget(self.csv_h_min, 0, 1)
        hsv_l.addWidget(QLabel("to"), 0, 2); self.csv_h_max = QSpinBox(); self.csv_h_max.setRange(0,179); self.csv_h_max.setValue(90); hsv_l.addWidget(self.csv_h_max, 0, 3)
        hsv_l.addWidget(QLabel("S:"), 1, 0); self.csv_s_min = QSpinBox(); self.csv_s_min.setRange(0,255); self.csv_s_min.setValue(27); hsv_l.addWidget(self.csv_s_min, 1, 1)
        hsv_l.addWidget(QLabel("to"), 1, 2); self.csv_s_max = QSpinBox(); self.csv_s_max.setRange(0,255); self.csv_s_max.setValue(255); hsv_l.addWidget(self.csv_s_max, 1, 3)
        hsv_l.addWidget(QLabel("V:"), 2, 0); self.csv_v_min = QSpinBox(); self.csv_v_min.setRange(0,255); self.csv_v_min.setValue(27); hsv_l.addWidget(self.csv_v_min, 2, 1)
        hsv_l.addWidget(QLabel("to"), 2, 2); self.csv_v_max = QSpinBox(); self.csv_v_max.setRange(0,255); self.csv_v_max.setValue(255); hsv_l.addWidget(self.csv_v_max, 2, 3)
        csv_v.addWidget(hsv_g)

        self.csv_gen_btn = QPushButton("Generate CSV")
        self.csv_gen_btn.setObjectName("primary")
        self.csv_gen_btn.clicked.connect(self._csv_generate)
        csv_v.addWidget(self.csv_gen_btn)

        self.csv_pbar = QProgressBar()
        self.csv_pbar.setFixedHeight(14)
        self.csv_pbar.setVisible(False)
        csv_v.addWidget(self.csv_pbar)

        self.csv_result = QLabel("")
        csv_v.addWidget(self.csv_result)
        csv_v.addStretch()
        self.tabs.addTab(csv_tab, "CSV Export")

    # ── Folder loading ──
    def _load_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select export folder containing GNDVI/, DEM/, etc.")
        if not folder:
            return
        self._export_dir = folder
        self.folder_lbl.setText(Path(folder).name)

    def _has_band(self, sub: str) -> bool:
        if not self._export_dir:
            return False
        d = Path(self._export_dir) / sub
        return d.exists() and any(d.glob("*.tif"))

    def _get_gndvi(self) -> np.ndarray:
        """Load all GNDVI tiles as a 1D array."""
        return self._load_tiles_1d("GNDVI")

    def _load_tiles_1d(self, sub: str) -> np.ndarray:
        """Load all tiles from subfolder and return concatenated 1D array."""
        if not self._export_dir:
            return np.array([])
        base = Path(self._export_dir)
        for d in [base / sub, base / sub.lower(), base / sub.upper()]:
            if d.exists():
                arrays = []
                for p in sorted(d.glob("*.tif")):
                    try:
                        img = np.array(Image.open(p)).astype(float)
                        if img.max() > 1.0:
                            img = (img / 65535.0) * 2.0 - 1.0
                        arrays.append(img.ravel())
                    except Exception:
                        continue
                if arrays:
                    return np.concatenate(arrays)
        return np.array([])

    # ── Canopy Cover ──
    def _compute_cc(self):
        if not self._export_dir:
            QMessageBox.warning(self, "No folder", "Load an export folder first.")
            return
        self.cc_progress.setVisible(True)
        self.cc_progress.setValue(0)
        QApplication.processEvents()

        gndvi = self._get_gndvi()
        if gndvi.size == 0:
            self.cc_result.setText("No GNDVI tiles found.")
            self.cc_progress.setVisible(False)
            return

        th = self.cc_thresh.value()
        canopy = (gndvi > th).astype(float)
        pct = np.mean(canopy) * 100
        self.cc_result.setText(f"Canopy cover: {pct:.1f}%  (threshold {th:.2f})")

        # Save per-tile results
        out_dir = Path(self._export_dir) / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.cc_progress.setValue(100)
        self.cc_progress.setVisible(False)

    # ── DTM Loading ──
    def _load_dtm(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select DTM file", "", "GeoTIFF (*.tif)")
        if path:
            self._dtm_path = path
            self.ch_result.setText(f"DTM loaded: {Path(path).name}")

    # ── ML Training ──
    def _train_model(self):
        code = self.code_edit.toPlainText()
        if not code.strip():
            return
        try:
            X_list = []
            y_list = []
            # Build simple dataset from loaded tiles
            if self._export_dir:
                gndvi = self._get_gndvi()
                if gndvi.size > 0:
                    X_list.append(gndvi)
            if X_list:
                X = np.column_stack(X_list)
                y = np.random.rand(X.shape[0])
            else:
                self.ml_result.setText("No data — load folder first")
                return
            exec(code, {"np": np, "X": X, "y": y, "self": self, "sklearn": __import__("sklearn")})
            self.ml_result.setText("Model trained")
        except Exception as e:
            self.ml_result.setText(f"Error: {e}")

    # ── CSV Export ──
    def _csv_select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder containing Dem/, GNDVI/, GRVI/, WRDVI/, Optical/ subfolders")
        if not folder:
            return
        self._csv_dir = folder
        self.csv_folder_lbl.setText(Path(folder).name)

    def _csv_generate(self):
        if not hasattr(self, "_csv_dir") or not self._csv_dir:
            QMessageBox.warning(self, "No folder", "Select a data folder first.")
            return
        base = Path(self._csv_dir)
        self.csv_pbar.setVisible(True)
        self.csv_pbar.setValue(0)

        def tile_name(path):
            return Path(path).stem

        # Collect all tile names
        all_tile_names = set()
        for sub in [base / "DEM", base / "dem", base / "Dem",
                    base / "GNDVI", base / "gndvi",
                    base / "GRVI", base / "grvi",
                    base / "WDRVI", base / "wdrvi", base / "WRDVI", base / "wrdvi",
                    base / "Optical", base / "optical", base / "RGB", base / "rgb"]:
            if sub.exists():
                for p in sub.glob("*.tif"):
                    all_tile_names.add(tile_name(p))
        all_tile_names = sorted(all_tile_names)
        if not all_tile_names:
            self.csv_result.setText("No TIF files found.")
            self.csv_pbar.setVisible(False)
            return

        # DEM
        self.csv_result.setText("Processing DEM…")
        QApplication.processEvents()
        height_dict = {}
        for d in [base / "DEM", base / "dem", base / "Dem"]:
            if d.exists():
                for p in d.glob("*.tif"):
                    try:
                        img = np.array(Image.open(p)).astype(float)
                        cal = self.csv_dem_slope.value() * img.mean() + self.csv_dem_inter.value()
                        height_dict[tile_name(p)] = cal
                    except Exception:
                        continue
                break
        self.csv_pbar.setValue(25)

        # Indices
        self.csv_result.setText("Processing indices…")
        QApplication.processEvents()
        vi_dir_map = {"GNDVI": [base / "GNDVI", base / "gndvi"],
                      "GRVI":  [base / "GRVI",  base / "grvi"],
                      "WDRVI": [base / "WDRVI", base / "wdrvi", base / "WRDVI", base / "wrdvi"]}
        vi_thresh = {"GNDVI": self.csv_gndvi_th.value(),
                     "GRVI":  self.csv_grvi_th.value(),
                     "WDRVI": self.csv_wdrvi_th.value()}
        vi_dicts = {k: {} for k in vi_dir_map}
        for label, dirs in vi_dir_map.items():
            for d in dirs:
                if d.exists():
                    for p in d.glob("*.tif"):
                        try:
                            with rasterio.open(p) as src:
                                raw = src.read(1).astype(float)
                            if raw.dtype == np.uint16 or (raw.max() > 1.5 and raw.min() >= 0):
                                vi = (raw / 65535.0) * 2.0 - 1.0
                            else:
                                vi = raw
                            vals = vi[vi > vi_thresh[label]]
                            vi_dicts[label][tile_name(p)] = vals.mean() if vals.size > 0 else ""
                        except Exception:
                            continue
                    break
        self.csv_pbar.setValue(65)

        # Optical
        self.csv_result.setText("Processing Optical…")
        QApplication.processEvents()
        fcover_dict = {}
        for d in [base / "Optical", base / "optical", base / "RGB", base / "rgb"]:
            if d.exists():
                for p in d.glob("*.tif"):
                    try:
                        img = cv2.imread(str(p))
                        if img is None:
                            continue
                        total_pixels = img.size
                        black_pixels = np.sum(img == 0)
                        non_zero_pixels = total_pixels - black_pixels
                        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                        mask = cv2.inRange(hsv,
                            (self.csv_h_min.value(), self.csv_s_min.value(), self.csv_v_min.value()),
                            (self.csv_h_max.value(), self.csv_s_max.value(), self.csv_v_max.value()))
                        mask = cv2.medianBlur(mask, 3)
                        veg_pixels = cv2.countNonZero(mask)
                        fcover_dict[tile_name(p)] = (veg_pixels / (non_zero_pixels / 3)) * 100 if non_zero_pixels else 0
                    except Exception:
                        continue
                break
        self.csv_pbar.setValue(100)

        rows = []
        for name in all_tile_names:
            rows.append({
                "Tile name": name,
                "GNDVI": vi_dicts["GNDVI"].get(name, ""),
                "WDRVI": vi_dicts["WDRVI"].get(name, ""),
                "GRVI":  vi_dicts["GRVI"].get(name, ""),
                "F Cover": fcover_dict.get(name, ""),
                "Height": height_dict.get(name, ""),
            })
        df = pd.DataFrame(rows)
        out_csv = str(base / "Next.csv")
        df.to_csv(out_csv, index=False)
        self.csv_result.setText(f"✅ Generated: Next.csv ({len(rows)} tiles)")
        self.csv_pbar.setVisible(False)


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    sys.excepthook = lambda etype, value, tb: (
        print("".join(traceback.format_exception(etype, value, tb)),
              file=open("crash.log", "w"))
    )
    app = QApplication(sys.argv)
    app.setApplicationName("GIS Tile Cutter")
    win = MainWindow()
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

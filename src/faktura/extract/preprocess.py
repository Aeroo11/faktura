"""Image clean-up for phone photos of invoices, before the vision model reads them.

Only photos are touched; clean PDFs and flatbed scans are returned unchanged, so the
accuracy already reached on them cannot regress.

Steps for a photo:
1. Find the paper (largest bright region) and, if it has four corners, warp it flat.
2. Even out the lighting. The light level is estimated from *paper* pixels only and fitted
   with a smooth quadratic surface. A plain "divide by blurred background" would also
   flatten dark printed areas such as a navy header band, turning white-on-dark text
   (vendor name, NPWP) into white-on-white.
3. Grayscale (removes the colour cast) and a percentile contrast stretch.
"""

from __future__ import annotations

import cv2
import numpy as np

# A page counts as a photo when the paper's light level varies this much across the page
# (max/min of the fitted surface). Measured on the synthetic v2 set: clean PDFs and
# scans stay at or below 1.18, phone photos start at 1.49. "Paper does not fill the frame"
# was tried as a second criterion and rejected: a full-width navy header band splits the
# white area of a clean PDF, so 26 of 101 clean pages were wrongly called photos.
LIGHTING_RATIO_MIN = 1.3
# Within a photo, warp only when the paper visibly does not fill the frame.
PAPER_FILL_MAX = 0.97
GRID = 32  # light level is estimated on a coarse grid of GRID x GRID-ish blocks


def _decode(png: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image")
    return img


def _encode(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("could not encode image")
    return buf.tobytes()


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Top-left, top-right, bottom-right, bottom-left."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def find_paper(gray: np.ndarray) -> tuple[np.ndarray | None, float]:
    """Return (4 corners or None, share of the frame covered by the paper)."""
    blur = cv2.GaussianBlur(gray, (7, 7), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = max(5, gray.shape[1] // 60)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, 0.0
    paper = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(paper)
    fill = area / (gray.shape[0] * gray.shape[1])
    # Extreme points of the hull, not approxPolyDP: when the paper runs off an image edge
    # its outline has more than four vertices, but its four extreme corners still work.
    hull = cv2.convexHull(paper).reshape(-1, 2).astype(np.float32)
    s, d = hull.sum(axis=1), np.diff(hull, axis=1).ravel()
    corners = np.array(
        [hull[np.argmin(s)], hull[np.argmin(d)], hull[np.argmax(s)], hull[np.argmax(d)]],
        dtype=np.float32,
    )
    if cv2.contourArea(corners) < 0.85 * area:  # degenerate quad: do not warp
        return None, fill
    return corners, fill


def warp(img: np.ndarray, corners: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = corners
    w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(
        img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def paper_light_surface(gray: np.ndarray) -> np.ndarray:
    """Smooth estimate of how bright blank paper is at each pixel."""
    h, w = gray.shape
    gw, gh = GRID, max(GRID, int(GRID * h / w))
    # Per block, a high percentile approximates the paper (text and bands are darker).
    small = cv2.resize(gray, (gw * 8, gh * 8), interpolation=cv2.INTER_AREA)
    blocks = small.reshape(gh, 8, gw, 8).transpose(0, 2, 1, 3).reshape(gh, gw, 64)
    level = np.percentile(blocks, 90, axis=2)
    # Blocks much darker than typical paper are bands, stamps, or background: skip them.
    keep = level > 0.6 * np.median(level)
    ys, xs = np.nonzero(keep)
    x, y = xs / (gw - 1), ys / (gh - 1)
    a = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)
    coef, *_ = np.linalg.lstsq(a, level[keep], rcond=None)
    xx, yy = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    surf = coef[0] + coef[1] * xx + coef[2] * yy + coef[3] * xx**2 + coef[4] * xx * yy
    surf += coef[5] * yy**2
    return np.clip(surf, 1, 255)


def even_lighting(gray: np.ndarray, surface: np.ndarray) -> np.ndarray:
    out = gray.astype(np.float32) / surface * 255.0
    lo, hi = np.percentile(out, [1, 99.5])
    out = (out - lo) / max(hi - lo, 1) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def preprocess(png: bytes, warp_paper: bool = False) -> tuple[bytes, dict[str, float | bool]]:
    """Return (image bytes, metadata). Non-photos come back byte-for-byte unchanged.

    `warp_paper` is off by default. On the synthetic phone photos, uneven lighting makes
    the dark half of the paper look like background, so the detected paper outline is
    wrong and the warp crops away real content (in visual checks it cut off the vendor
    name, NPWP, invoice number, and bank account). Lighting correction alone never
    removes content.
    """
    img = _decode(png)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    surface = paper_light_surface(gray)
    lighting_ratio = float(surface.max() / surface.min())
    corners, fill = find_paper(gray)
    is_photo = lighting_ratio >= LIGHTING_RATIO_MIN
    meta: dict[str, float | bool] = {
        "photo": is_photo,
        "lighting_ratio": round(lighting_ratio, 3),
        "paper_fill": round(fill, 3),
        "warped": False,
    }
    if not is_photo:
        return png, meta
    if warp_paper and corners is not None and fill < PAPER_FILL_MAX:
        gray = warp(gray, corners)
        meta["warped"] = True
    return _encode(even_lighting(gray, paper_light_surface(gray))), meta

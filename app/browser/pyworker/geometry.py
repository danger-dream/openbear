"""CSS viewport geometry, including projective iframe transforms."""

from __future__ import annotations

from .common import BrowserError


def transform(point, source, destination):
    """Map between quadrilaterals via a homography; no axis-aligned iframe assumption."""
    rows = []
    for i in range(0, 8, 2):
        x, y, u, v = source[i], source[i + 1], destination[i], destination[i + 1]
        rows.extend(([x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]))
    for col in range(8):
        pivot = max(range(col, 8), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1e-10:
            raise BrowserError("element_not_visible")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        rows[col] = [v / scale for v in rows[col]]
        for row in range(8):
            if row != col:
                scale = rows[row][col]
                rows[row] = [v - scale * w for v, w in zip(rows[row], rows[col])]
    a, b, c, d, e, f, g, h = [row[8] for row in rows]
    x, y = point["x"], point["y"]
    denominator = g * x + h * y + 1
    if abs(denominator) < 1e-10:
        raise BrowserError("element_not_visible")
    return {"x": (a * x + b * y + c) / denominator, "y": (d * x + e * y + f) / denominator}


def viewport_quad(width, height):
    return [0, 0, width, 0, width, height, 0, height]


def clickable_point(quads, width, height):
    """Clip actual content quads, not their axis-aligned bounding rectangles."""
    for quad in quads:
        polygon = list(zip(quad[::2], quad[1::2]))
        for axis, boundary, lower in (
            (0, 0, True),
            (0, width, False),
            (1, 0, True),
            (1, height, False),
        ):
            result = []
            for i, end in enumerate(polygon):
                start = polygon[i - 1]
                sin = start[axis] >= boundary if lower else start[axis] <= boundary
                ein = end[axis] >= boundary if lower else end[axis] <= boundary
                if sin != ein:
                    ratio = (boundary - start[axis]) / (end[axis] - start[axis])
                    result.append(tuple(start[j] + ratio * (end[j] - start[j]) for j in range(2)))
                if ein:
                    result.append(end)
            polygon = result
            if not polygon:
                break
        area = (
            abs(
                sum(
                    polygon[i - 1][0] * p[1] - p[0] * polygon[i - 1][1]
                    for i, p in enumerate(polygon)
                )
            )
            / 2
        )
        if area > 1:
            return {
                "x": sum(p[0] for p in polygon) / len(polygon),
                "y": sum(p[1] for p in polygon) / len(polygon),
            }
    return None

"""Точная геометрия отрезков в целочисленных координатах KiCad."""

from fractions import Fraction


def orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def intersects(first, second):
    a, b = first
    c, d = second
    if any(max(min(a[i], b[i]), min(c[i], d[i])) > min(max(a[i], b[i]), max(c[i], d[i])) for i in (0, 1)):
        return False
    return orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0


def point_segment_squared_distance(point, segment):
    a, b = segment
    dx, dy = b[0] - a[0], b[1] - a[1]
    px, py = point[0] - a[0], point[1] - a[1]
    length_squared = dx * dx + dy * dy
    along = px * dx + py * dy
    if along <= 0:
        return px * px + py * py
    if along >= length_squared:
        return (point[0] - b[0]) ** 2 + (point[1] - b[1]) ** 2
    return Fraction((dx * py - dy * px) ** 2, length_squared)


def segment_squared_distance(first, second):
    if intersects(first, second):
        return 0
    return min(
        *(point_segment_squared_distance(point, second) for point in first),
        *(point_segment_squared_distance(point, first) for point in second),
    )

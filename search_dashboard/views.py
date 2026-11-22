"""Render backend route points on an illustrative site. No route inference."""
import html
from .domain import CAMERAS, SITE_LINKS

def site_map(case, *, numbered=False):
    points = case.route
    last = points[-1].get("camera_id") if points else None
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 410" role="img" aria-label="Camera map">',
           '<rect width="680" height="410" rx="8" fill="#f8fafc"/>',
           '<path d="M70 160 H590 V350 H290 V250 H70 Z M290 70 H590 V160 H290 Z" fill="#edf2f7" stroke="#cbd5e1" stroke-width="2"/>',
           '<text x="28" y="35" fill="#536579" font-size="13" font-family="sans-serif">Demo site / Floor 1</text>']
    for a, b in SITE_LINKS:
        x, y = CAMERAS[a], CAMERAS[b]
        svg.append(f'<line x1="{x["x"]}" y1="{x["y"]}" x2="{y["x"]}" y2="{y["y"]}" stroke="#a7b6c8" stroke-width="3" stroke-dasharray="6 7"/>')
    for p, q in zip(points, points[1:]):
        if p.get("camera_id") not in CAMERAS or q.get("camera_id") not in CAMERAS:
            continue
        a, b = CAMERAS[p["camera_id"]], CAMERAS[q["camera_id"]]
        svg.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" stroke="#2563eb" stroke-width="5"/>')
    for key, c in CAMERAS.items():
        color = "#2563eb" if key == last else "#64748b"
        if key == last:
            svg.append(f'<circle cx="{c["x"]}" cy="{c["y"]}" r="28" fill="none" stroke="#bfdbfe" stroke-width="7"/>')
        svg.append(f'<circle cx="{c["x"]}" cy="{c["y"]}" r="19" fill="{color}"/><text x="{c["x"]}" y="{c["y"]+5}" text-anchor="middle" fill="white" font-family="sans-serif" font-size="15">{key}</text><text x="{c["x"]}" y="{c["y"]+47}" text-anchor="middle" fill="#334155" font-family="sans-serif" font-size="13">{html.escape(c["name"])}</text>')
    if numbered:
        # Stack repeated sightings at the same camera without moving their anchor.
        repeats = {}
        for index, point in enumerate(points, 1):
            camera_id = point.get("camera_id")
            camera = CAMERAS.get(camera_id)
            if not camera:
                continue
            offset = repeats.get(camera_id, 0)
            repeats[camera_id] = offset + 1
            x, y = camera["x"] + offset * 34, camera["y"] - 40
            final = index == len(points)
            fill, ink = ("#1766da", "#fff") if final else ("#fff", "#125cc4")
            svg.append(
                f'<g transform="translate({x},{y})">'
                f'<path d="M-15 -16 H15 V12 H5 L0 19 L-5 12 H-15 Z" fill="{fill}" '
                f'stroke="#1766da" stroke-width="2.4" stroke-linejoin="round"/>'
                f'<text x="0" y="4" text-anchor="middle" fill="{ink}" '
                f'font-size="15" font-weight="700" font-family="sans-serif">{index}</text></g>'
            )
    svg.append('<text x="28" y="389" fill="#536579" font-size="12" font-family="sans-serif">Blue: backend route · Ring: latest route point · Not to scale</text></svg>')
    return "".join(svg).encode()

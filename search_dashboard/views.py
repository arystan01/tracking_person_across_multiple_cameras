"""Presentation helpers; no external dependencies or service calls."""
import html
from .domain import CAMERAS, EDGES
from .service import route


def site_map(case):
    points = route(case)
    last = points[-1]["camera_id"]
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 410" role="img" aria-label="Illustrative four-camera site map">',
           '<rect width="680" height="410" rx="18" fill="#0e1c2b"/>',
           '<path d="M70 160 H590 V350 H290 V250 H70 Z M290 70 H590 V160 H290 Z" fill="#192c3e" stroke="#345069" stroke-width="2"/>',
           '<text x="28" y="35" fill="#91a9bf" font-size="13" font-family="sans-serif">DEMO SITE / FLOOR 01 · NOT TO SCALE</text>']
    for a, b, _ in EDGES:
        x, y = CAMERAS[a], CAMERAS[b]
        svg.append(f'<line x1="{x["x"]}" y1="{x["y"]}" x2="{y["x"]}" y2="{y["y"]}" stroke="#42607a" stroke-width="4" stroke-dasharray="6 7"/>')
    for p, q in zip(points, points[1:]):
        a, b = CAMERAS[p["camera_id"]], CAMERAS[q["camera_id"]]
        color = "#ffba66" if q["travel_conflict"] else "#54d7bd"
        svg.append(f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" stroke="{color}" stroke-width="5"/>')
    for key, c in CAMERAS.items():
        color = "#54d7bd" if key == last else "#7794b1"
        if key == last:
            svg.append(f'<circle cx="{c["x"]}" cy="{c["y"]}" r="27" fill="none" stroke="{color}" opacity="0.4" stroke-width="8"/>')
        svg.append(f'<circle cx="{c["x"]}" cy="{c["y"]}" r="19" fill="{color}"/><text x="{c["x"]}" y="{c["y"]+5}" text-anchor="middle" fill="#0e1c2b" font-family="sans-serif" font-size="15" font-weight="bold">{key}</text><text x="{c["x"]}" y="{c["y"]+47}" text-anchor="middle" fill="#d3e0ed" font-family="sans-serif" font-size="13">{html.escape(c["name"])}</text>')
    svg.append('<text x="28" y="389" fill="#91a9bf" font-size="12" font-family="sans-serif">Teal: reviewed route · Amber: travel conflict · Ring: latest route point</text></svg>')
    return ''.join(svg).encode()

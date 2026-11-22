"""Loopback HTTP adapter for the original demo workflow's React interface."""
import base64
import binascii
import hmac
import io
import json
import os
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from .backend import DemoControlAPI
from .domain import CAMERAS
from .report import PdfExport
from .service import STOP_LABELS, can_review, status_label
from .storage import SQLiteStore
from .views import site_map

ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")


def reference_image(fields):
    if fields.get("no_photo") is True:
        return None
    encoded = fields.get("reference")
    if not encoded:
        raise ValueError("Upload a reference photo or enable the demo option.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 5_000_000:
            raise ValueError("Use an image smaller than 5 MB.")
        rotation = fields["rotation"]
        crop = fields["crop"]
        if rotation not in (0, 90, 180, 270) or len(crop) != 4 or not all(
            isinstance(value, int) and 0 <= value <= 100 for value in crop
        ):
            raise ValueError("Choose a valid rotation and crop.")
        with Image.open(io.BytesIO(raw)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB").rotate(-rotation, expand=True)
        width, height = image.size
        left, top, right, bottom = (
            int(width * crop[0] / 100), int(height * crop[1] / 100),
            int(width * crop[2] / 100), int(height * crop[3] / 100),
        )
        if left >= right or top >= bottom:
            raise ValueError("Select a crop with positive width and height.")
        image = image.crop((left, top, right, bottom))
        image.thumbnail((1400, 1400))
        output = io.BytesIO()
        image.save(output, "JPEG")
        return output.getvalue()
    except (binascii.Error, Image.DecompressionBombError, OSError, TypeError, KeyError) as exc:
        raise ValueError("Unable to read this reference photo.") from exc


def json_result(value, status=200):
    return status, "application/json", json.dumps(value).encode()


class DemoHttp:
    """Keep simulation and data storage in the same services Streamlit uses."""

    def __init__(self, api, secret):
        self.api = api
        self.secret = secret

    def view(self, case):
        detail = case.to_dict()
        detail.pop("simulation")
        for candidate in detail["candidates"]:
            candidate.pop("metadata")
        return {
            **detail, "status_label": status_label(case), "can_review": can_review(case),
            "stop_label": STOP_LABELS.get(case.stop_reason),
            "map_svg": site_map(case, numbered=True).decode(),
        }

    def dispatch(self, method, path, authorization="", fields=None):
        fields = fields or {}
        if method == "POST" and path == "/demo-api/login":
            if fields.get("username") == "operator" and fields.get("password") == "trace-demo":
                return json_result({"token": self.secret, "cameras": {key: item["name"] for key, item in CAMERAS.items()}})
            return json_result({"detail": "Incorrect username or password."}, 401)
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + self.secret).encode()):
            return json_result({"detail": "Sign in to the demo."}, 401)
        if method == "POST" and path == "/demo-api/cases":
            if fields.get("sample") is True:
                known = datetime.now(KST) - timedelta(minutes=15)
                case = self.api.create_case("Demo case", "operator", "A", known.isoformat())
            else:
                case = self.api.create_case(fields["title"], "operator", fields["camera"],
                                            fields["known"], reference_image(fields), fields.get("notes", ""))
            return json_result(self.view(case), 201)
        camera = re.fullmatch(r"/demo-api/videos/([ABCD])", path)
        if method == "GET" and camera:
            clip = ROOT / "assets" / f"camera-{camera[1]}.mp4"
            if clip.is_file():
                return 200, "video/mp4", clip.read_bytes()
        match = re.fullmatch(r"/demo-api/cases/([a-z0-9_-]+)(?:/(report|decision|close))?", path)
        if match:
            case_id, action = match.groups()
            if method == "GET" and action is None:
                return json_result(self.view(self.api.get_case_view(case_id)))
            if method == "GET" and action == "report":
                return 200, "application/pdf", PdfExport().encode(self.api.get_case_view(case_id))
            if method == "POST" and action == "decision":
                return json_result(self.view(self.api.submit_decision(case_id, fields["candidate"], fields["decision"], "operator")))
            if method == "POST" and action == "close":
                return json_result(self.view(self.api.close_case(case_id, "operator")))
            if method == "DELETE" and action is None:
                self.api.delete_case(case_id, "operator")
                return 204, "application/json", b""
        return json_result({"detail": "Resource unavailable."}, 404)


def main():
    api = DemoControlAPI(SQLiteStore(os.environ.get("TRACE_DB_PATH", str(ROOT / "data" / "trace.sqlite3"))))
    router = DemoHttp(api, os.environ["TRACE_DEMO_SESSION"])

    class Handler(BaseHTTPRequestHandler):
        def serve_request(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 7_000_000:
                    raise ValueError("Request exceeds the demo upload limit.")
                fields = json.loads(self.rfile.read(size)) if size else {}
                if not isinstance(fields, dict):
                    raise ValueError("Invalid form.")
                status, kind, body = router.dispatch(self.command, self.path, self.headers.get("Authorization", ""), fields)
            except (ValueError, KeyError, TypeError) as exc:
                status, kind, body = json_result({"detail": str(exc)}, 400)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = serve_request

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        print(server.server_port, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()

"""Server-side client for the JAVES-AI Controls API."""

from urllib.parse import quote, urlsplit

import requests


class ControlsError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def case_path(case_id):
    return f"/v1/cases/{quote(case_id, safe='')}"


class ControlsClient:
    def __init__(self, base_url, token=None):
        url = urlsplit(base_url.strip())
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise ControlsError("Set TRACE_CONTROLS_URL to the HTTP(S) address of Controls.")
        self.base_url = url.geturl().rstrip("/")
        self.token = token

    def request(self, method, path, *, payload=None, key=None, media_type=None):
        if not path.startswith("/v1/") or "\\" in path:
            raise ControlsError("Controls returned an invalid resource path.")
        headers = {"Accept": media_type or "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if key:
            headers["Idempotency-Key"] = key
        try:
            response = requests.request(
                method, self.base_url + path, headers=headers, json=payload,
                timeout=(3, 12), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ControlsError("Controls is offline or did not respond. Try again.") from exc
        if not 200 <= response.status_code < 300:
            messages = {
                401: "Sign-in failed or your session expired.",
                403: "Your account does not have permission for this action.",
                404: "This case or resource is unavailable.",
                409: "Controls rejected the request because of a conflict.",
                422: "Controls rejected the submitted fields.",
            }
            message = messages.get(response.status_code, "Controls could not complete the request.")
            if response.status_code in {409, 422}:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and detail:
                    message = f"{message} {detail}"
            raise ControlsError(message, response.status_code)
        if media_type:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != media_type:
                raise ControlsError("Controls returned an unexpected media type.")
            return response.content
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ControlsError("Controls returned an invalid response.") from exc

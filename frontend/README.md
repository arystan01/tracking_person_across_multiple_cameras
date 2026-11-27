# JAVES-AI React Dashboard

The browser talks only to the same-origin `javes-dashboard` BFF. It never stores a
Controls bearer token or reads worker, database, filesystem, Routing, Perception,
Clip, Retention, Site Config, or Archive APIs directly.

For local development, start the BFF on port 8090 and run:

```bash
npm ci
npm run dev
```

`JAVES_DASHBOARD_BFF_URL` may point Vite at another BFF URL. Production assets are
built with `npm run build` and served by the Python BFF from `frontend/dist`.

The case flow is: authenticated reference upload → opaque `upload_id` → case
creation → candidate review → server-rendered route SVG → clips/export/retention.
Appearance similarity is a technical ranking signal and is never presented as an
identity probability.

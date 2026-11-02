from __future__ import annotations

__all__ = ["init_state", "step", "render_route_maps", "step_and_render"]


def __getattr__(name: str):
    if name in ("init_state", "step"):
        from .step import init_state, step  # noqa: F811
        return {"init_state": init_state, "step": step}[name]
    if name == "render_route_maps":
        from .render import render_route_maps
        return render_route_maps
    if name == "step_and_render":
        from .worker import step_and_render
        return step_and_render
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

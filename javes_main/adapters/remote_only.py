"""Fail-closed placeholders for ports owned by remote workers."""


class RemoteWorkerOnlyPort:
    def __init__(self, component: str) -> None:
        self.component = component

    def __getattr__(self, name: str):
        def unavailable(*_args, **_kwargs):
            raise RuntimeError(
                f"{self.component}.{name} is available only through the worker HTTP API"
            )

        return unavailable

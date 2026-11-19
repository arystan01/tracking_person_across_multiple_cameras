from __future__ import annotations

import ast
import importlib
import sys
import tomllib
from pathlib import Path

import setuptools


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(package: Path) -> set[str]:
    modules: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                modules.add(node.module)
    return modules


def test_old_component_source_paths_are_absent() -> None:
    assert not list((ROOT / "routing").rglob("*.py"))
    assert not list((ROOT / "javes_routing_api").rglob("*.py"))
    for old_path in (
        "javes_perception/worker.py",
        "javes_perception/runtime.py",
        "javes_perception/state.py",
        "javes_retention/worker.py",
        "javes_retention/runtime.py",
        "javes_retention/repository.py",
    ):
        assert not (ROOT / old_path).exists()


def test_core_import_boundaries() -> None:
    routing = imported_modules(ROOT / "javes_routing" / "core")
    assert not any(
        name.split(".")[0]
        in {"fastapi", "httpx", "sqlalchemy", "sqlite3", "pathlib", "requests"}
        for name in routing
    )

    perception = imported_modules(ROOT / "javes_perception" / "core")
    assert not any(
        name == "javes_main"
        or name.startswith("javes_main.")
        or name == "javes_workers"
        or name.startswith("javes_workers.")
        for name in perception
    )

    retention = imported_modules(ROOT / "javes_retention" / "core")
    assert not any(
        name.split(".")[0] in {"fastapi", "httpx", "requests", "javes_workers"}
        for name in retention
    )


def test_contracts_depend_only_on_pydantic_and_standard_library() -> None:
    imports = imported_modules(ROOT / "javes_contracts")
    allowed_project_prefix = "javes_contracts"
    third_party = {
        name.split(".")[0]
        for name in imports
        if not name.startswith(allowed_project_prefix)
        and name.split(".")[0] not in sys.stdlib_module_names
    }
    assert third_party == {"pydantic"}


def test_preserved_console_entrypoints_are_importable() -> None:
    targets = {
        "javes-routing-api": "javes_routing.service.cli:main",
        "javes-routing-demo": "javes_routing.service.demo:main",
        "javes-routing-bench": "javes_routing.service.bench:main",
        "javes-perception-worker": "javes_perception.runtime.worker_cli:main",
        "javes-retention-worker": "javes_retention.runtime.worker_cli:main",
        "javes-nvidia-archive-api": "javes_archive.cli:api_main",
        "javes-nvidia-archive-inspect": "javes_archive.cli:inspect_main",
    }
    configuration = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = configuration["project"]["scripts"]
    for command, target in targets.items():
        assert scripts[command] == target
        module_name, attribute = target.split(":", 1)
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute))


def test_setuptools_discovers_new_packages_only() -> None:
    packages = set(setuptools.find_packages(where=ROOT))
    assert {
        "javes_routing.core",
        "javes_routing.service",
        "javes_perception.core",
        "javes_perception.runtime",
        "javes_retention.core",
        "javes_retention.runtime",
        "javes_archive",
    } <= packages
    assert "routing" not in packages
    assert "javes_routing_api" not in packages

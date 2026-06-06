"""Architecture guardrails for the staged lib/ package migration."""
from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
ARCHITECTURE_MAP = ROOT / "docs" / "architecture" / "lib-map.md"
ROOT_COMPATIBILITY_WRAPPERS = {
    "axis.py": "geometry.axis",
    "inventory.py": "registries.inventory",
    "materials.py": "registries.materials",
    "metrics.py": "registries.metrics",
    "printers.py": "registries.printers",
    "printing.py": "slicing.printing",
    "projects_registry.py": "registries.projects",
}


def _python_files(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_command_modules_stay_under_commands_package() -> None:
    offenders: list[str] = []
    for path in _python_files(LIB):
        if path.parent == LIB / "commands":
            continue
        for node in _tree(path).body:
            if isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if "COMMAND" in names:
                    offenders.append(str(path.relative_to(ROOT)))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "COMMAND":
                    offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, "command modules must live under lib/commands: " + ", ".join(offenders)


def test_package_init_files_import_stdlib_or_local_modules_only() -> None:
    stdlib = set(sys.stdlib_module_names)
    allowed_external = {"typing_extensions"}
    offenders: list[str] = []

    for path in sorted(LIB.rglob("__init__.py")):
        for node in _tree(path).body:
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported = [node.module.split(".", 1)[0]]
            else:
                continue

            for name in imported:
                if name not in stdlib and name not in allowed_external:
                    offenders.append(f"{path.relative_to(ROOT)} imports {name}")

    assert not offenders, "__init__.py files must stay import-light: " + ", ".join(offenders)


def test_root_lib_modules_are_documented_in_architecture_map() -> None:
    assert ARCHITECTURE_MAP.exists(), "docs/architecture/lib-map.md must document root lib modules"
    text = ARCHITECTURE_MAP.read_text(encoding="utf-8")

    missing = []
    for path in sorted(LIB.glob("*.py")):
        marker = f"`{path.name}`"
        if marker not in text:
            missing.append(path.name)

    assert not missing, (
        "root lib/*.py modules must be compatibility shims or documented in "
        f"{ARCHITECTURE_MAP.relative_to(ROOT)}: {', '.join(missing)}"
    )


def test_root_compatibility_wrappers_are_declared_and_minimal() -> None:
    text = ARCHITECTURE_MAP.read_text(encoding="utf-8")
    for filename, target in ROOT_COMPATIBILITY_WRAPPERS.items():
        path = LIB / filename
        tree = _tree(path)
        assert f"`{filename}` \u2192 `{target}`" in text
        imports_target = any(
            isinstance(node, ast.ImportFrom)
            and node.module == target
            and all(alias.name != "*" for alias in node.names)
            for node in tree.body
        )
        assert imports_target, f"{filename} must explicitly re-export from {target}"
        function_defs = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        class_defs = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        assert not function_defs and not class_defs, (
            f"{filename} is a compatibility wrapper; implementation belongs in {target}"
        )

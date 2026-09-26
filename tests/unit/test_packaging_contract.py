from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_async_sqlalchemy_runtime_installs_greenlet_extra() -> None:
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    dependencies = pyproject["project"]["dependencies"]
    assert any(
        dependency.lower().startswith("sqlalchemy[asyncio]")
        for dependency in dependencies
    )


def test_ci_type_checker_version_is_reproducible() -> None:
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    development_dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    assert "mypy==2.3.0" in development_dependencies

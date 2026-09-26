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

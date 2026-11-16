"""Record the five live API scenes with a disposable, network-isolated beta."""

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
VHS = "memoryos-vhs-recording:local"


def docker(*args, capture=False):
    return subprocess.run(
        ["docker", *args], cwd=ROOT, check=True, text=True, capture_output=capture
    ).stdout


def main():
    if not shutil.which("docker"):
        raise SystemExit("Install Docker with Compose to run the local API and VHS: https://docs.docker.com/get-docker/")
    project = f"memoryos-gif-{uuid4().hex[:10]}"
    compose = (
        "compose", "-p", project, "-f", "beta/compose.yml",
        "-f", "examples/demo/compose.recording.yml",
    )
    docker("build", "-f", "examples/demo/vhs.Dockerfile", "-t", VHS, ".")
    with TemporaryDirectory(prefix="memoryos-recording-") as directory:
        scratch = Path(directory)
        assets = scratch / "assets"
        assets.mkdir()
        shutil.copyfile(ROOT / "docs/assets/demo.tape", assets / "demo.tape")
        publisher = None
        try:
            docker(*compose, "build", "api")
            docker(*compose, "up", "-d", "--wait", "postgres", "redis", "qdrant")
            docker(*compose, "up", "-d", "localstack")
            docker(*compose, "run", "--rm", "--no-deps", "api", "alembic", "upgrade", "head")
            docker(*compose, "up", "-d", "--wait", "api", "worker", "background")
            docker(*compose, "exec", "-T", "api", "python", "beta/bootstrap.py", "--create",
                   "--output", "/tmp/demo-credentials.json")
            credentials = scratch / "recording-credentials.local.json"
            docker(*compose, "cp", "api:/tmp/demo-credentials.json", str(credentials))
            tenant = json.loads(credentials.read_text())["tenant_id"]
            publisher = docker(*compose, "run", "--rm", "-d", "--no-deps", "-e",
                               f"LOCAL_VECTOR_TENANT_ID={tenant}", "publisher", capture=True).strip()
            api_id = docker(*compose, "ps", "-q", "api", capture=True).strip()
            runner = (
                "run", "--rm", "--network", f"container:{api_id}", "--shm-size=512m",
                "--mount", f"type=bind,src={ROOT},dst=/vhs,readonly",
                "--mount", f"type=bind,src={assets},dst=/vhs/docs/assets",
                "--mount", f"type=bind,src={scratch},dst=/recording",
                "--workdir", "/vhs", "-e", "MEMORYOS_RECORDING_DIR=/recording",
            )
            # Prime the worker and vector index on a separate synthetic account.
            docker(*runner, "-e", "MEMORYOS_DEMO_MOCK=1", "--entrypoint", "python",
                   VHS, "examples/demo/1_write.py")
            docker(*runner, VHS, "docs/assets/demo.tape")
            state = json.loads((scratch / "recording-state.local.json").read_text())
            if not state.get("recorded"):
                raise RuntimeError("The fifth scene did not complete. The checked-in GIF was preserved.")
            # VHS renders its GIF at the encoder's rate. Resample to 12 fps.
            docker("run", "--rm", "--network", "none",
                   "--mount", f"type=bind,src={assets},dst=/assets", "--workdir", "/assets",
                   "--entrypoint", "ffmpeg", VHS, "-v", "error", "-i", "demo.gif",
                   "-filter_complex", "fps=12,split[a][b];[a]palettegen[p];[b][p]paletteuse",
                   "-loop", "0", "demo-12fps.gif")
            gif = assets / "demo-12fps.gif"
            if not 0 < gif.stat().st_size < 6_000_000:
                raise RuntimeError("The recording must be under 6 MB.")
            shutil.copyfile(gif, ROOT / "docs/assets/demo.gif")
        finally:
            try:
                if publisher:
                    docker("stop", publisher)
            finally:
                docker(*compose, "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()

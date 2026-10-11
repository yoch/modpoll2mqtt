"""Install a built wheel in an isolated environment, outside the source tree."""

import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    (wheel,) = (root / "dist").glob("*.whl")
    with tempfile.TemporaryDirectory(prefix="modpoll-wheel-") as temporary:
        directory = Path(temporary)
        environment = directory / "venv"
        venv.create(environment, with_pip=True)
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME")
        }

        def run(arguments):
            return subprocess.run(
                arguments,
                cwd=directory,
                env=clean_env,
                check=True,
                capture_output=True,
                text=True,
            )

        run([str(python), "-m", "pip", "install", str(wheel)])
        executable = bin_dir / ("modpoll.exe" if os.name == "nt" else "modpoll")
        assert "--check-config" in run([str(executable), "--help"]).stdout
        config = directory / "device.csv"
        config.write_text(
            "device,smoke,1\npoll,holding_register,0,1,BE_BE\nref,value,0,uint16,r\n"
        )
        run([str(executable), "--check-config", "--config", str(config)])
        # Exercise the serial extra without opening a port.
        run([str(python), "-m", "pip", "install", f"{wheel}[serial]"])
        run(
            [
                str(python),
                "-c",
                "import serial; import modpoll; print(modpoll.__version__)",
            ]
        )
    print("Installed wheel: CLI, offline configuration and serial extra passed")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Master controller for the scheduled 4:00 PM dashboard refresh."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"


def emit(message: str, log_handle) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    log_handle.write(line + "\n")
    log_handle.flush()


def run_step(step_name: str, script_name: str, log_handle) -> None:
    script_path = SCRIPT_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Missing script: {script_path}")

    emit(f"Starting {step_name}: {script_name}", log_handle)
    command = [sys.executable, str(script_path), "--no-browser"]
    emit(f"Command: {' '.join(command)}", log_handle)

    process = subprocess.Popen(
        command,
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        clean_line = line.rstrip()
        if clean_line:
            emit(f"{step_name} | {clean_line}", log_handle)

    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)

    emit(f"Completed {step_name} successfully.", log_handle)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now().strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"daily_run_{run_date}.log"

    with open(log_path, "a", encoding="utf-8") as log_handle:
        emit("=== Daily dashboard run started ===", log_handle)
        emit(f"Log file: {log_path}", log_handle)
        try:
            run_step("Downloader", "eod_batch_downloader.py", log_handle)
            run_step("Universe Drift", "rs_universe_generator.py", log_handle)
            run_step("RS 3T", "rs_matrix_3T.py", log_handle)
            run_step("RS Crypto", "rs_matrix_crypto.py", log_handle)
            run_step("Breadth", "market_breadth.py", log_handle)
        except subprocess.CalledProcessError as exc:
            emit(
                f"ERROR: {Path(exc.cmd[1]).name} failed with exit code {exc.returncode}. "
                "Aborting downstream steps.",
                log_handle,
            )
            return 1
        except Exception as exc:
            emit(f"ERROR: {exc}", log_handle)
            return 1

        emit("SUCCESS: Dashboard Ready", log_handle)
        emit("=== Daily dashboard run finished ===", log_handle)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

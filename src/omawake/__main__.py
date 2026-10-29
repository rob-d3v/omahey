# SPDX-License-Identifier: Apache-2.0
import os
import sys

from .cli import main


def _quiet_stdout() -> None:
    # Point stdout at /dev/null so the interpreter's final flush stays quiet
    # ("Exception ignored on flushing sys.stdout: BrokenPipeError").
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def run(argv=None):
    try:
        try:
            rc = main(argv)
        except SystemExit as e:     # argparse --help / errors
            rc = e.code
        sys.stdout.flush()          # surface a broken pipe here, not at exit
        return rc
    except BrokenPipeError:         # e.g. `omawake status | head -1`
        _quiet_stdout()
        return 0


if __name__ == "__main__":
    sys.exit(run())

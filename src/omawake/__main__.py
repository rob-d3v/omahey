# SPDX-License-Identifier: Apache-2.0
import sys

from .cli import main

try:
    sys.exit(main())
except BrokenPipeError:      # e.g. `omawake status | head`
    sys.exit(0)

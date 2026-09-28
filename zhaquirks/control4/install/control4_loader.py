"""Load the Control4 quirks from a `control4/` subfolder of custom_quirks_path.

Copy this file to the ROOT of your custom quirks folder, next to (not inside)
the `control4/` folder:

    /config/custom_zha_quirks/
    ├── control4_loader.py      <- this file
    └── control4/               <- zhaquirks/control4 from the repository

and point ZHA at the root:

    zha:
      custom_quirks_path: /config/custom_zha_quirks

ZHA only loads .py files directly inside custom_quirks_path, plus subfolders
that are Python packages (have an __init__.py). control4/ deliberately is
not a package — its modules import each other by plain name (`import
c4_helpers`) — so this file puts control4/ on sys.path and imports c4_hooks,
which in turn imports and registers every Control4 device quirk.
"""

import os
import sys

_CONTROL4_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control4")
if _CONTROL4_DIR not in sys.path:
    sys.path.insert(0, _CONTROL4_DIR)

import c4_hooks  # noqa: E402,F401

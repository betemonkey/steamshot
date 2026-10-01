import os
import sys

if not __package__:
    # run as `python path/to/steam_snapshot`: make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from steam_snapshot.cli import main  # noqa: E402

sys.exit(main())

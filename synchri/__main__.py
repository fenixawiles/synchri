"""Allow ``python -m synchri`` as an alias for the ``synchri`` script."""

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())

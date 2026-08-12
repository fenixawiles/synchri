"""Allow ``python -m aidapter`` as an alias for the ``aidapter`` script."""

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())

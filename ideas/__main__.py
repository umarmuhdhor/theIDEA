"""`python -m ideas` — same entry point as ./run.sh."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

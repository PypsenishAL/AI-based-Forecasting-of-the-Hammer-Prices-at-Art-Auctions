"""Compatibility entry point for christies history."""

import sys

from auction_pipeline.sources.christies import results as christies_history
from auction_pipeline import history


main = christies_history.main


def __getattr__(name):
    for module in (christies_history, history,):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


if __name__ == "__main__":
    sys.exit(main())

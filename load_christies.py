"""Compatibility entry point for christies."""

import sys

from auction_pipeline.sources.christies import catalogue as christies
from auction_pipeline import common
from auction_pipeline import snapshots


main = christies.main


def __getattr__(name):
    for module in (christies, common, snapshots,):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


if __name__ == "__main__":
    sys.exit(main())

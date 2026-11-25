"""Compatibility entry point for sothebys."""

import sys

from auction_pipeline.sources.sothebys import catalogue as sothebys
from auction_pipeline.sources.sothebys import results


main = sothebys.main


def __getattr__(name):
    for module in (sothebys, results):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


if __name__ == "__main__":
    sys.exit(main())

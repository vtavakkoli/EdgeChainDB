"""Backwards-compatible wrapper for the generic validation runner.

Use `python -m edgechaindb.validation` or the `edgechain-validation`
console command for new workflows.
"""

from .validation import main


if __name__ == "__main__":
    main()

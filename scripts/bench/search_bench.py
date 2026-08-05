"""Compatibility launcher for the packaged retrieval benchmark."""

import sys

from kb import retrieval_eval


if __name__ == "__main__":
    raise SystemExit(retrieval_eval.main())

# Keep imports and monkeypatches aimed at the old module path attached to the
# implementation module. A star re-export would copy mutable globals such as
# METRIC_DEFINITIONS and GROUND_TRUTH, making old tests patch the wrong object.
sys.modules[__name__] = retrieval_eval

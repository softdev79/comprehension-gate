"""Run the gate CLI from any directory: python path/to/comprehension_gate.py <command> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

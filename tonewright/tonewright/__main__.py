import sys

from .app import main

if __name__ == "__main__":          # importing this module (tools, tests) must never start the application
    sys.exit(main())

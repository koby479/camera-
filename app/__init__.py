from app._build import BUILD

VERSION = "1.1"                       # bump by hand when something notable changes
__version__ = f"v{VERSION}.{BUILD}"   # e.g. v1.1.28; the last number is the release build (0 = running from source)

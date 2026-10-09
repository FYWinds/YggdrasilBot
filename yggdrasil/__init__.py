import os

# Release images get the git tag (vX.Y.Z / sha-xxxx) as YGG_VERSION at build time; pyproject's
# version stays fixed so releases don't touch uv.lock and bust the dependency layer cache.
__version__ = os.environ.get("YGG_VERSION") or "dev"

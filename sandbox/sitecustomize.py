# Seed matplotlib's font cache into the per-workspace MPLCONFIGDIR. The root fs is read-only and HOME is an
# empty volume, so without this the first `import matplotlib` in every workspace rescans all system fonts.
# Costs one stat per interpreter start once the directory exists.
import os


def _seed_mpl_cache() -> None:
    dst = os.environ.get("MPLCONFIGDIR")
    src = "/opt/ygg/mpl-cache"
    if not dst or os.path.isdir(dst) or not os.path.isdir(src):
        return
    try:
        import shutil

        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError:
        pass


_seed_mpl_cache()
del _seed_mpl_cache

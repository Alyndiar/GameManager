"""Game Backup Manager package."""

try:
    # Registers AVIF support in Pillow when pillow-avif-plugin is installed.
    import pillow_avif  # noqa: F401
except ImportError:
    pass

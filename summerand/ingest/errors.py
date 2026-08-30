class PermanentFeedError(RuntimeError):
    """The feed can't work with this configuration (missing key, plan not entitled, endpoint
    gone). Services log it once and exit 0 instead of crash-looping."""

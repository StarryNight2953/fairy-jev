"""Independent two-stage TypeSafe Jev controller for FAIRY.

The SDK is imported lazily; inventory and scripted runs need no credentials.
"""


def __getattr__(name):
    if name == "JevAgent":
        from .agent import JevAgent
        return JevAgent
    raise AttributeError(name)


__all__ = ["JevAgent"]

"""External integrations behind user-level credentials.

Layer 1, like ``app/llm``: adapters that talk to a provider and expose a small async port to the
tools. The wiring module (``app/main.py``) is the only place that constructs them.
"""

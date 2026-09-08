"""Derivations over the store: match, cluster, judge, and find the gaps.

Nothing here fetches from the network except `extract`, which is the one pass
that calls an LLM. The rest is deliberately lexical: no model to download, no
API key needed, and a result that can be explained to somebody.
"""

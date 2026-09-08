"""Hackathon idea mining: winning projects on one side, unmet needs on the other.

The two halves meet in `analysis`: `matcher` and `gap` ask how much of the
project corpus already answers a mined pain point, and what is left over.

    scrapers/  where the data comes from — projects, and social pain points
    store/     the SQLite file both halves share
    analysis/  everything that reads the store and derives something
    web/       the Flask dashboard over the same file
    cli.py     the command line all of it hangs off
"""

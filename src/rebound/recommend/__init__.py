"""SPEC §14: a recommendation service that reuses the strategies, simulating nothing.

A sibling of the simulator, not an extension of it. Nothing in this package may modify
`engine/`, `harness/`, `population/` or `strategies/`; it is a second entry point into
the strategy classes of SPEC §4, driven by fields a real merchant supplies rather than by
a generated book.
"""

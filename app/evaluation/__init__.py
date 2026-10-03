"""M17: scenario specifications, their reference labels, seeding and the evaluation split.

Each case is a specification (config/eval_scenarios/*.yaml): the records it needs, materialized
as ``SEED-`` rows in the database, the customer's true intent and final slot values, special
conditions, and a customer script for M18. Its reference label comes from running the
Deterministic Policy Engine on the specification, never on the text (policy §16).
"""

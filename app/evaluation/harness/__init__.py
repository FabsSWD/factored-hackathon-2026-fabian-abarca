"""M18 evaluation harness: the system and a baseline on the evaluation split (policy §16).

The system runs whole, behind its real API, in this process: a customer simulator logs in and
plays each case's script, faults are injected per conversation when the case asks for them
(ESC-10, ESC-11), and every conversation is compared with the reference label of M17. See
docs/evaluation.md for the results and scripts/run_evaluation.py for the command.
"""

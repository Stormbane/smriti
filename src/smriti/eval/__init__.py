"""Search evaluation for smriti — labeled cases, metrics, and reports."""

from smriti.eval.cases import SearchCase
from smriti.eval.runner import eval_sandbox, run_search_cases

__all__ = [
    "SearchCase",
    "eval_sandbox",
    "run_search_cases",
]

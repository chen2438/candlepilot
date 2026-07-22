"""Independent, advisory market analysis.

This package deliberately does not import the execution or risk engines.  Its
output is research material and can never become an order by accident.
"""

from candlepilot.analysis.models import MarketAnalysis

__all__ = ["MarketAnalysis"]

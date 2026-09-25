"""Cron-side intake code (fetch, tools, rules, runner).

Guardian check 20 (owner C10): nothing in this package imports default_storage or
writes TransmissionReport, TCRow or any other core table. It writes only intake and
agent tables, through agent.gate.perform. Only intake/confirm.py (web process, admin
Confirm) writes a TransmissionReport.
"""

"""Merchant dashboard notifications: new order, escalation, out of stock.

Cross-cutting: orders and the agent emit rows; the dashboard reads them.
French copy is a frontend concern — `data` holds raw fields only.
"""

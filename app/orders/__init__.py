"""Orders, order items, stock_movements, and payment-method flagging.

Stock decrement on order creation must use a row-level lock in a
transaction (SELECT ... FOR UPDATE) — see .cursor/rules/general.mdc.
"""

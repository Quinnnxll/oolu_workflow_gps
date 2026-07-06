from __future__ import annotations

# Decision-log (roadmap §9) money knobs. Documented defaults, configurable per
# deployment/region; centralized here for review and sign-off before real money
# moves. Amounts are in integer micro-units (1 currency unit = 1_000_000 micros).

DEFAULT_RHO = 0.30  # platform commission rate on net contribution
DEFAULT_HOLDBACK_DAYS = 14  # H: delay before earnings become payable
DEFAULT_RESERVE_FRACTION = 0.10  # R: fraction held against chargeback/refund risk
DEFAULT_RISK_WINDOW_DAYS = 90  # W: chargeback window; older earnings release
DEFAULT_MIN_PAYOUT_MICROS = 20_000_000  # T: minimum payable balance ($20.00)
DEFAULT_MU_MAX = 2.0  # reputation multiplier ceiling

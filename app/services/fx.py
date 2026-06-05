# Approximate FX rates to USD. ~₹95 = $1 (June 2026). Update periodically
# or wire to a live FX API (exchangerate.host, ECB, etc.).
_RATES_TO_USD = {"USD": 1.0, "INR": 1 / 95.0, "EUR": 1.08, "GBP": 1.27}

def to_usd(amount, currency):
    rate = _RATES_TO_USD.get((currency or "USD").upper(), 1.0)
    return float(amount or 0) * rate
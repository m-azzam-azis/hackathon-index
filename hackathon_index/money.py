"""Currency amounts in prize text. Deliberately rough: the precise cash/credits/swag
split comes later from an LLM pass over the stored prize descriptions."""

import re

# Approximate USD rates; good enough for a $1k threshold. "$" is assumed USD.
USD_RATE = {
    "USD": 1.0, "CAD": 0.72, "AUD": 0.65, "SGD": 0.77, "NZD": 0.60, "HKD": 0.13,
    "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "JPY": 0.0067, "CNY": 0.14,
    "IDR": 0.000063, "MYR": 0.22, "PHP": 0.018, "NGN": 0.00065, "KRW": 0.00073,
    "CHF": 1.12, "BRL": 0.18, "MXN": 0.055, "PKR": 0.0036, "AED": 0.27, "ZAR": 0.055,
}

_SYMBOLS = [  # longest first
    ("CA$", "CAD"), ("C$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"), ("S$", "SGD"),
    ("NZ$", "NZD"), ("HK$", "HKD"), ("US$", "USD"), ("R$", "BRL"), ("Rp", "IDR"),
    ("RM", "MYR"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"),
    ("¥", "JPY"), ("₱", "PHP"), ("₦", "NGN"), ("₩", "KRW"),
]
_SYM_RE = "|".join(re.escape(s) for s, _ in _SYMBOLS)
_CODE_RE = "|".join(USD_RATE)

AMOUNT_RE = re.compile(
    rf"(?P<sym>{_SYM_RE})\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<mult>[kKmM]\b)?"
    rf"|(?P<num2>\d[\d,]*(?:\.\d+)?)\s?(?P<mult2>[kKmM]\b)?\s?(?P<code>{_CODE_RE})\b"
)
NON_CASH_RE = re.compile(r"credit|worth of|voucher|gift card|subscription|tier|license|tokens?\b", re.I)


def find_amounts(text: str) -> list[tuple[float, str, bool]]:
    """All amounts in `text` as (amount, currency, looks_non_cash)."""
    out = []
    for m in AMOUNT_RE.finditer(text or ""):
        if m.group("sym"):
            cur = dict(_SYMBOLS)[m.group("sym")]
            num, mult = m.group("num"), m.group("mult")
        else:
            cur, num, mult = m.group("code"), m.group("num2"), m.group("mult2")
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        if mult:
            value *= 1_000 if mult.lower() == "k" else 1_000_000
        # Only the words right after the amount, up to the next separator, describe it.
        after = re.split(r"[&,;\n]|\band\b", text[m.end(): m.end() + 30], maxsplit=1)[0]
        out.append((value, cur, bool(NON_CASH_RE.search(after))))
    return out


def to_usd(amount: float | None, currency: str | None) -> float | None:
    if amount is None or not currency:
        return None
    rate = USD_RATE.get(currency)
    return round(amount * rate, 2) if rate else None

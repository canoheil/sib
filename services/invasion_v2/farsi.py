"""Persian digit formatting for the Invasion v2 admin surfaces.

Audit rule (docs/invasion-rework/PERSIAN-TEXT-AUDIT.md, phase 3): quantities the
panel derives (counts, durations, HP, difficulty, percentages) are printed with
Persian digits and the Persian decimal separator, while identifiers an admin
retypes or greps in logs and the database (group ids, invasion ids, region code,
enemy code, status codes, config keys, ISO timestamps) stay Latin.
"""
from sib.services.sib_claim import fa_digits


def fa_number(value) -> str:
    """Persian digits for a displayed quantity: 3600 -> ۳۶۰۰ و 2.5 -> ۲٫۵.

    Floats (and strings with a decimal point) use the Persian decimal separator;
    everything else is translated as-is, so large integer ids stay exact.
    """
    if isinstance(value, float) or (isinstance(value, str) and "." in value):
        try:
            return fa_digits(f"{float(value):g}").replace(".", "٫")
        except ValueError:
            return fa_digits(value)
    return fa_digits(value)


def fa_percent(fraction: float, decimals: int = 1) -> str:
    """Persian percent with the Persian percent sign: 0.452 -> ۴۵٫۲٪."""
    return fa_number(f"{fraction * 100:.{decimals}f}") + "٪"

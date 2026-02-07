import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import AccountType, Position


def _clean_numeric(value: str) -> Decimal:
    """Strip $, commas, and whitespace from a numeric string, return Decimal."""
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned or cleaned == "n/a" or cleaned == "--":
        return Decimal("0")
    # Handle negative values in parens like ($1,234.56)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return Decimal(cleaned)


def _normalize_symbol(symbol: str) -> str:
    """Strip trailing ** and whitespace from a ticker symbol."""
    return symbol.strip().rstrip("*")


def _detect_account_type(
    account_name: str, account_mappings: dict[str, AccountType] | None = None
) -> AccountType:
    """Detect account type from account name string."""
    if account_mappings:
        for substr, acct_type in account_mappings.items():
            if substr.upper() in account_name.upper():
                return acct_type

    name_upper = account_name.upper()
    if "ROTH IRA" in name_upper:
        return AccountType.ROTH_IRA
    if "TRADITIONAL IRA" in name_upper or "ROLLOVER IRA" in name_upper:
        return AccountType.TRADITIONAL_IRA
    if "ROTH" in name_upper and "401" in name_upper:
        return AccountType.ROTH_401K
    if "401" in name_upper:
        return AccountType.FOUR_01K
    if "HSA" in name_upper:
        return AccountType.HSA
    if "INDIVIDUAL" in name_upper or "TOD" in name_upper or "TAXABLE" in name_upper:
        return AccountType.TAXABLE
    return AccountType.TAXABLE


def _is_account_header(row: list[str]) -> bool:
    """Check if a CSV row is an account header (account name, rest mostly empty)."""
    if not row or not row[0].strip():
        return False
    # Account headers have a non-empty first cell and the rest are empty
    non_empty = [cell.strip() for cell in row[1:] if cell.strip()]
    return len(non_empty) == 0


def _is_footer_or_total(row: list[str]) -> bool:
    """Check if a row is a footer/totals row to skip."""
    first = row[0].strip().lower() if row else ""
    if first.startswith("the totals"):
        return True
    if first == "" and len(row) > 1:
        # Check if this is a totals summary row (no symbol but has a value)
        symbol = row[1].strip() if len(row) > 1 else ""
        if not symbol:
            return True
    return False


def _detect_csv_format(header_row: list[str]) -> str:
    """Detect which Fidelity CSV format we're dealing with.

    Returns 'per_row_account' if each row has Account Number/Name columns,
    or 'header_account' if accounts appear as header rows.
    """
    headers = [h.strip() for h in header_row]
    if "Account Name" in headers and "Account Number" in headers:
        return "per_row_account"
    return "header_account"


def _safe_decimal(value: str) -> Decimal:
    try:
        return _clean_numeric(value) if value else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _safe_decimal_or_none(value: str) -> Decimal | None:
    try:
        return _clean_numeric(value) if value else None
    except InvalidOperation:
        return None


def parse_fidelity_csv(
    path: Path, account_mappings: dict[str, AccountType] | None = None
) -> list[Position]:
    """Parse a Fidelity positions CSV into a list of Position objects.

    Supports two Fidelity CSV formats:
    - Per-row accounts: each row has Account Number and Account Name columns
    - Header accounts: account names appear as separator rows
    """
    positions: list[Position] = []
    header_indices: dict[str, int] = {}
    csv_format: str | None = None

    # State for header_account format
    current_account: str | None = None
    current_account_type: AccountType = AccountType.TAXABLE

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)

        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue

            first_cell = row[0].strip()

            # Stop at footer/totals section
            if first_cell.lower().startswith("the totals"):
                break

            # Detect header row (either format) — only the first one
            if not header_indices and (
                first_cell in ("Account Name/Number", "Account Number")
            ):
                for i, col in enumerate(row):
                    col_stripped = col.strip()
                    if col_stripped:
                        header_indices[col_stripped] = i
                csv_format = _detect_csv_format(row)
                continue

            if not header_indices:
                continue

            def _get(col_name: str) -> str:
                idx = header_indices.get(col_name)
                if idx is not None and idx < len(row):
                    return row[idx].strip()
                return ""

            # For header_account format, handle account header and footer rows
            if csv_format == "header_account":
                if _is_account_header(row):
                    current_account = first_cell
                    current_account_type = _detect_account_type(
                        current_account, account_mappings
                    )
                    continue
                if _is_footer_or_total(row):
                    continue

            # Get symbol — skip rows without one
            symbol_raw = _get("Symbol")
            if not symbol_raw:
                continue

            ticker = _normalize_symbol(symbol_raw)
            description = _get("Description")

            # Determine account name and type
            if csv_format == "per_row_account":
                account_name = _get("Account Name")
                if not account_name:
                    account_name = _get("Account Number") or "Unknown Account"
                account_type = _detect_account_type(account_name, account_mappings)
            else:
                account_name = current_account or "Unknown Account"
                account_type = current_account_type

            quantity = _safe_decimal(_get("Quantity"))
            price = _safe_decimal(_get("Last Price"))
            market_value = _safe_decimal(_get("Current Value"))
            cost_basis = _safe_decimal_or_none(_get("Cost Basis Total"))

            positions.append(
                Position(
                    account_name=account_name,
                    account_type=account_type,
                    ticker=ticker,
                    description=description,
                    quantity=quantity,
                    price=price,
                    market_value=market_value,
                    cost_basis_total=cost_basis,
                )
            )

    return positions

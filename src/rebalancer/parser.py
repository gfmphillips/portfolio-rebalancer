import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import AccountType, Position, TaxLot, Transaction


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
            if ticker.lower() in ("pending activity",):
                continue
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


def _detect_transaction_format(headers: list[str]) -> str:
    """Detect transaction CSV format.

    Returns 'simplified' for Date,Account,Ticker,Action,Shares
    or 'fidelity' for Fidelity's native export format.
    """
    normalized = [h.strip().lower() for h in headers]
    if "ticker" in normalized and "action" in normalized:
        return "simplified"
    if "symbol" in normalized and ("run date" in normalized or "date" in normalized):
        return "fidelity"
    raise ValueError(
        f"Unrecognized transaction CSV format. Headers: {headers}. "
        "Expected either simplified (Date,Account,Ticker,Action,Shares) "
        "or Fidelity native format."
    )


def _normalize_action(raw: str) -> str | None:
    """Normalize a transaction action string to BUY or SELL, or None if unrecognized."""
    upper = raw.strip().upper()
    if upper in ("BUY", "BOUGHT", "YOU BOUGHT"):
        return "BUY"
    if upper in ("SELL", "SOLD", "YOU SOLD"):
        return "SELL"
    if "REINVEST" in upper or "DIVIDEND" in upper:
        return "BUY"  # dividend reinvestments are buys
    return None


def parse_transactions(path: Path) -> list[Transaction]:
    """Parse a transaction history CSV into a list of Transaction objects.

    Supports two formats:
    - Simplified: Date,Account,Ticker,Action,Shares
    - Fidelity native: Run Date,Account,Action,Symbol,...,Quantity,...
    """
    transactions: list[Transaction] = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)

        # Find header row
        header_indices: dict[str, int] = {}
        csv_format: str | None = None
        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue
            # Try to detect format from this row
            try:
                csv_format = _detect_transaction_format(row)
                for i, col in enumerate(row):
                    col_stripped = col.strip()
                    if col_stripped:
                        header_indices[col_stripped.lower()] = i
                break
            except ValueError:
                continue

        if csv_format is None:
            raise ValueError("Could not find a valid header row in transaction CSV.")

        def _get(col_name: str) -> str:
            idx = header_indices.get(col_name.lower())
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return ""

        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue

            if csv_format == "simplified":
                date = _get("date")
                account = _get("account")
                ticker = _normalize_symbol(_get("ticker"))
                action_raw = _get("action")
                shares_raw = _get("shares")
            else:
                # Fidelity format
                date = _get("run date") or _get("date")
                account = _get("account")
                ticker = _normalize_symbol(_get("symbol"))
                action_raw = _get("action")
                shares_raw = _get("quantity")

            if not ticker or not action_raw:
                continue

            action = _normalize_action(action_raw)
            if action is None:
                continue

            try:
                shares = abs(_clean_numeric(shares_raw)) if shares_raw else Decimal("0")
            except InvalidOperation:
                continue

            if shares <= 0:
                continue

            transactions.append(
                Transaction(
                    date=date,
                    account_name=account,
                    ticker=ticker,
                    action=action,
                    shares=shares,
                )
            )

    return transactions


def parse_lots(path: Path) -> dict[tuple[str, str], list[TaxLot]]:
    """Parse a tax lot CSV into a dict keyed by (account_name, ticker).

    Expected CSV headers: Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare
    Strips ** from tickers, handles $ and , in cost basis.
    """
    lots: dict[tuple[str, str], list[TaxLot]] = {}

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)

        # Find header row
        header_indices: dict[str, int] = {}
        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue
            normalized = [h.strip().lower() for h in row]
            if "account" in normalized and "ticker" in normalized:
                for i, col in enumerate(row):
                    col_stripped = col.strip().lower()
                    if col_stripped:
                        header_indices[col_stripped] = i
                break

        if not header_indices:
            raise ValueError(
                "Could not find valid header row in lot CSV. "
                "Expected: Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare"
            )

        def _get(col_name: str) -> str:
            idx = header_indices.get(col_name.lower())
            if idx is not None and idx < len(row):
                return row[idx].strip()
            return ""

        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue

            account = _get("account")
            ticker = _normalize_symbol(_get("ticker"))
            acq_date = _get("acquisitiondate")
            shares_raw = _get("shares")
            cost_raw = _get("costbasispershare")

            if not ticker or not shares_raw:
                continue

            try:
                shares = abs(_clean_numeric(shares_raw))
            except InvalidOperation:
                continue
            if shares <= 0:
                continue

            try:
                cost_basis = _clean_numeric(cost_raw) if cost_raw else Decimal("0")
            except InvalidOperation:
                cost_basis = Decimal("0")

            lot = TaxLot(
                acquisition_date=acq_date,
                shares=shares,
                cost_basis_per_share=cost_basis,
            )
            key = (account, ticker)
            lots.setdefault(key, []).append(lot)

    return lots


_FIDELITY_DATE_RE = re.compile(r"^[A-Z][a-z]{2}-\d{2}-\d{4}$")
_ACCOUNT_TRAILING_ID_RE = re.compile(r"x\d+$")
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

_LOT_HEADER_LINES = [
    "Acquired",
    "Term",
    "$ Total gain/loss",
    "% Total gain/loss",
    "Current value",
    "Quantity",
    "Average cost basis",
    "Cost basis total",
]


def _fidelity_date_to_iso(date_str: str) -> str:
    """Convert Fidelity date format (e.g. 'Mar-04-2025') to ISO 'YYYY-MM-DD'."""
    dt = datetime.strptime(date_str, "%b-%d-%Y")
    return dt.strftime("%Y-%m-%d")


def _clean_account_name(raw: str) -> str:
    """Strip trailing Fidelity account ID fragments (e.g. 'Rollover IRAx02' -> 'Rollover IRA')."""
    return _ACCOUNT_TRAILING_ID_RE.sub("", raw).strip()


def parse_fidelity_lots_paste(text: str) -> dict[tuple[str, str], list[TaxLot]]:
    """Parse copy-pasted Fidelity Positions page text into tax lot data.

    Returns a dict keyed by (account_name, ticker) -> list of TaxLot.
    Positions without expanded lot data are silently skipped.
    """
    lots: dict[tuple[str, str], list[TaxLot]] = {}
    lines = [line.strip() for line in text.splitlines()]
    n = len(lines)

    current_account = "Unknown Account"
    i = 0

    while i < n:
        line = lines[i]

        # Detect account header
        if line == "Account:":
            if i + 1 < n and lines[i + 1]:
                current_account = _clean_account_name(lines[i + 1])
            i += 2
            continue

        # Detect lot header block: 8 consecutive lines matching the header
        if (
            line == "Acquired"
            and i + 7 < n
            and lines[i + 1] == "Term"
        ):
            # Find the ticker by scanning backwards for a short all-caps line
            ticker = None
            for j in range(i - 1, max(i - 30, -1), -1):
                if _TICKER_RE.match(lines[j]):
                    ticker = _normalize_symbol(lines[j])
                    break

            if ticker is None:
                i += 8
                continue

            # Skip the 8 header lines
            i += 8

            # Parse lot rows: each lot is 8 lines starting with a date
            while i + 7 < n and _FIDELITY_DATE_RE.match(lines[i]):
                acq_date = _fidelity_date_to_iso(lines[i])
                # lines[i+1] = term, lines[i+2] = gain/loss $, lines[i+3] = gain/loss %
                # lines[i+4] = current value, lines[i+5] = quantity
                # lines[i+6] = avg cost basis, lines[i+7] = cost basis total
                shares = _clean_numeric(lines[i + 5])
                cost_basis_per_share = _clean_numeric(lines[i + 6])

                if shares > 0:
                    lot = TaxLot(
                        acquisition_date=acq_date,
                        shares=shares,
                        cost_basis_per_share=cost_basis_per_share,
                    )
                    key = (current_account, ticker)
                    lots.setdefault(key, []).append(lot)

                i += 8

            continue

        i += 1

    return lots


def attach_lots(
    positions: list[Position],
    lots: dict[tuple[str, str], list[TaxLot]],
) -> list[str]:
    """Attach parsed tax lots to matching Position objects.

    Uses fuzzy matching on account names (substring match, case-insensitive).
    Returns a list of warning strings for mismatches.
    """
    warnings: list[str] = []

    for pos in positions:
        # Try exact match first
        key = (pos.account_name, pos.ticker)
        matched_lots = lots.get(key)

        # Try fuzzy match: lot account is substring of position account or vice versa
        if matched_lots is None:
            for (lot_acct, lot_ticker), lot_list in lots.items():
                if lot_ticker != pos.ticker:
                    continue
                if (
                    lot_acct.upper() in pos.account_name.upper()
                    or pos.account_name.upper() in lot_acct.upper()
                ):
                    matched_lots = lot_list
                    break

        if matched_lots is None:
            continue

        pos.tax_lots = list(matched_lots)

        # Validate lot shares vs position quantity
        lot_total = sum(lot.shares for lot in matched_lots)
        if pos.quantity > 0 and lot_total != pos.quantity:
            diff = lot_total - pos.quantity
            warnings.append(
                f"{pos.account_name}/{pos.ticker}: lot shares ({lot_total}) "
                f"differ from position quantity ({pos.quantity}) by {diff:+}"
            )

    return warnings

"""Hong Leong Bank parser."""

import re
from datetime import datetime

from webapp.fallback_parsers.pdf_text import (
    TextItem,
    extract_text_items_from_pdf,
    group_text_items_into_rows,
)


class HongLeongBankParser:
    """Hong Leong Bank PrimeBiz Current Account parser."""

    def __init__(self):
        self._date_re = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")
        self._amount_re = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")

    @staticmethod
    def is_hlb_statement(pdf_bytes: bytes) -> bool:
        """Check if this is an HLB PrimeBiz statement."""
        items = extract_text_items_from_pdf(pdf_bytes)
        return any("HLB PRIMEBIZ CURRENT ACCOUNT" in item.text for item in items)

    def parse(self, pdf_bytes: bytes) -> list[dict]:
        """Parse HLB statement and return transactions as dicts."""
        items = extract_text_items_from_pdf(pdf_bytes)
        if not items:
            return []

        # Verify this is an HLB PrimeBiz statement
        if not any("HLB PRIMEBIZ CURRENT ACCOUNT" in item.text for item in items):
            return []

        rows = group_text_items_into_rows(items)
        header = self._find_transaction_header(rows)
        if header is None:
            return []

        header_y, anchors = header
        desc_left = anchors["desc"] - 20.0
        desc_right = anchors["deposit"] - 2.0

        ordered_rows: list[tuple[float, list[TextItem]]] = [
            (y, row_items) for y, row_items in rows.items() if y < header_y - 1.0
        ]
        ordered_rows.sort(key=lambda t: t[0], reverse=True)

        stop_phrases = (
            "Rebate Summary",
            "Closing Balance",
            "Total Withdrawals",
            "Total Deposits",
        )

        transactions: list[dict] = []
        current: dict | None = None

        for _, row_items in ordered_rows:
            full_line = " ".join(t.text for t in row_items)
            if any(p in full_line for p in stop_phrases):
                break

            date = self._extract_row_date(row_items, desc_left)
            desc = self._extract_description(
                row_items=row_items, desc_left=desc_left, desc_right=desc_right
            )
            deposit, withdrawal = self._extract_amounts(row_items, anchors)

            if date:
                if current is not None:
                    transactions.append(current)

                current = {
                    "date": date,
                    "description": desc,
                    "deposit": deposit,
                    "withdrawal": withdrawal,
                }
                continue

            # Continuation lines for the previous transaction
            if current is not None and desc:
                current["description"] = (current["description"] + " " + desc).strip()

        if current is not None:
            transactions.append(current)

        # Convert to dict format expected by ProcessedFile
        result: list[dict] = []
        for t in transactions:
            dep = t.get("deposit")
            wd = t.get("withdrawal")

            # Skip if both deposit and withdrawal exist (invalid)
            if dep and wd:
                continue

            if dep:
                amount = float(dep.replace(",", ""))
                polarity = "credit"
            elif wd:
                amount = -float(wd.replace(",", ""))
                polarity = "debit"
            else:
                continue

            # Date is already in YYYY-MM-DD format
            result.append({
                "date": t["date"],
                "description": t["description"],
                "amount": amount,
                "polarity": polarity,
            })

        return result

    def _find_transaction_header(
        self, rows: dict[float, list[TextItem]]
    ) -> tuple[float, dict[str, float]] | None:
        """Find the transaction table header."""
        required = ("Date", "Transaction Description", "Deposit", "Withdrawal", "Balance")
        for y, row_items in rows.items():
            by_text = {t.text: t.x for t in row_items}
            if all(k in by_text for k in required):
                return (
                    y,
                    {
                        "date": by_text["Date"],
                        "desc": by_text["Transaction Description"],
                        "deposit": by_text["Deposit"],
                        "withdrawal": by_text["Withdrawal"],
                        "balance": by_text["Balance"],
                    },
                )
        return None

    def _extract_row_date(self, row_items: list[TextItem], desc_left: float) -> str | None:
        """Extract date from row items."""
        for t in row_items:
            if t.x <= desc_left and (m := self._date_re.search(t.text)):
                day, month, year = m.group(0).split("-")
                return f"{year}-{month}-{day}"
        return None

    def _extract_description(
        self, *, row_items: list[TextItem], desc_left: float, desc_right: float
    ) -> str:
        """Extract transaction description."""
        parts: list[str] = [t.text for t in row_items if desc_left <= t.x < desc_right]
        return " ".join(parts).strip()

    def _extract_amounts(
        self, row_items: list[TextItem], anchors: dict[str, float]
    ) -> tuple[str | None, str | None]:
        """Extract deposit and withdrawal amounts."""
        deposit: str | None = None
        withdrawal: str | None = None
        for t in row_items:
            if not self._amount_re.fullmatch(t.text.strip()):
                continue

            if anchors["deposit"] - 2.0 <= t.x < anchors["withdrawal"] - 2.0:
                deposit = t.text
            elif anchors["withdrawal"] - 2.0 <= t.x < anchors["balance"] - 2.0:
                withdrawal = t.text

        return deposit, withdrawal

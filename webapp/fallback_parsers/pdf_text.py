import re
import zlib
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class TextItem:
    x: float
    y: float
    text: str


_NUM_TOKEN_RE = re.compile(
    br"^[+-]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?$"
)


def _decode_pdf_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c != 0x5C:  # backslash
            out.append(c)
            i += 1
            continue

        i += 1
        if i >= len(raw):
            break

        c2 = raw[i]
        if c2 == ord("n"):
            out.append(10)
            i += 1
        elif c2 == ord("r"):
            out.append(13)
            i += 1
        elif c2 == ord("t"):
            out.append(9)
            i += 1
        elif c2 == ord("b"):
            out.append(8)
            i += 1
        elif c2 == ord("f"):
            out.append(12)
            i += 1
        elif c2 in (ord("("), ord(")"), ord("\\")):
            out.append(c2)
            i += 1
        elif ord("0") <= c2 <= ord("7"):
            j = i
            oct_digits = bytearray()
            while j < len(raw) and len(oct_digits) < 3 and ord("0") <= raw[j] <= ord("7"):
                oct_digits.append(raw[j])
                j += 1
            out.append(int(oct_digits, 8) & 0xFF)
            i = j
        else:
            out.append(c2)
            i += 1

    return out.decode("latin1", errors="replace")


def _tokenize_pdf_content_stream(data: bytes):
    i = 0
    whitespace = b" \t\r\n\x0c\x00"
    while i < len(data):
        c = data[i]
        if c in whitespace:
            i += 1
            continue
        if c == 0x25:  # % comment
            j = data.find(b"\n", i)
            if j == -1:
                return
            i = j + 1
            continue
        if c == 0x28:  # literal string
            depth = 1
            i += 1
            buf = bytearray()
            while i < len(data) and depth > 0:
                ch = data[i]
                if ch == 0x5C:  # escape
                    buf.append(ch)
                    i += 1
                    if i < len(data):
                        buf.append(data[i])
                        i += 1
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf.append(ch)
                i += 1
            yield ("str", bytes(buf))
            continue
        if c == 0x5B:  # array
            depth = 1
            i += 1
            buf = bytearray()
            while i < len(data) and depth > 0:
                ch = data[i]
                if ch == 0x5C:
                    buf.append(ch)
                    i += 1
                    if i < len(data):
                        buf.append(data[i])
                        i += 1
                    continue
                if ch == 0x5B:
                    depth += 1
                elif ch == 0x5D:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf.append(ch)
                i += 1
            yield ("arr", bytes(buf))
            continue
        if c == 0x2F:  # name
            j = i + 1
            while j < len(data) and data[j] not in whitespace:
                if data[j] in b"()[]<>{}/%":
                    break
                j += 1
            yield ("name", data[i:j])
            i = j
            continue

        j = i
        while j < len(data) and data[j] not in whitespace:
            if data[j] in b"()[]<>{}/%":
                break
            j += 1
        yield ("tok", data[i:j])
        i = j


def extract_text_items_from_pdf(pdf_bytes: bytes) -> list[TextItem]:
    items: list[TextItem] = []
    for stream in _extract_flate_streams(pdf_bytes):
        if b"BT" not in stream:
            continue
        items.extend(_extract_text_items_from_content_stream(stream))
    return items


def group_text_items_into_rows(
    items: list[TextItem],
    *,
    y_tolerance: float = 1.8,
) -> dict[float, list[TextItem]]:
    rows: dict[float, list[TextItem]] = defaultdict(list)
    row_ys: list[float] = []

    for item in sorted(items, key=lambda t: (-t.y, t.x)):
        key: float | None = None
        for ky in row_ys:
            if abs(ky - item.y) <= y_tolerance:
                key = ky
                break

        if key is None:
            row_ys.append(item.y)
            key = item.y

        rows[key].append(item)

    # Keep items within each row ordered left-to-right
    return {y: sorted(row_items, key=lambda t: t.x) for y, row_items in rows.items()}


def _extract_flate_streams(pdf_bytes: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for m in re.finditer(br"stream\r?\n", pdf_bytes):
        start = m.end()
        end = pdf_bytes.find(br"endstream", start)
        if end == -1:
            continue
        raw = pdf_bytes[start:end]
        try:
            streams.append(zlib.decompress(raw))
        except Exception:
            continue
    return streams


def _extract_text_items_from_content_stream(stream: bytes) -> list[TextItem]:
    items: list[TextItem] = []
    stack: list[object] = []

    in_text = False
    x = 0.0
    y = 0.0

    for kind, val in _tokenize_pdf_content_stream(stream):
        if kind != "tok":
            stack.append((kind, val))
            continue

        if _NUM_TOKEN_RE.match(val):
            stack.append(float(val))
            continue

        op = val.decode("latin1")
        if op == "BT":
            in_text = True
            stack.clear()
            continue
        if op == "ET":
            in_text = False
            stack.clear()
            continue
        if not in_text:
            stack.clear()
            continue

        if op == "Tm" and len(stack) >= 6:
            x = float(stack[-2])
            y = float(stack[-1])
            stack.clear()
            continue

        if op == "Td" and len(stack) >= 2:
            x += float(stack[-2])
            y += float(stack[-1])
            stack.clear()
            continue

        if op == "Tj":
            if stack and isinstance(stack[-1], tuple) and stack[-1][0] == "str":
                text = _decode_pdf_literal(stack[-1][1]).strip()
                if text:
                    items.append(TextItem(x=x, y=y, text=text))
            stack.clear()
            continue

        if op == "TJ":
            if stack and isinstance(stack[-1], tuple) and stack[-1][0] == "arr":
                arr_raw: bytes = stack[-1][1]
                for sm in re.finditer(br"\((.*?)\)", arr_raw):
                    text = _decode_pdf_literal(sm.group(1)).strip()
                    if text:
                        items.append(TextItem(x=x, y=y, text=text))
            stack.clear()
            continue

        stack.clear()

    return items


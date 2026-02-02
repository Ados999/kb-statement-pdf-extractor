import re
import unicodedata
from pathlib import Path
import pdfplumber
import pandas as pd


# --- Input / output ---
PDF_PATH = "statement_kb.pdf"
OUTPUT_CSV = "transakce.csv"


# --- Regexes ---
DATE_RE = re.compile(r"^\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})")
HEADER_DATE_RE = r"\d{1,2}\.\s+\d{1,2}\.\s+\d{4}"
AMOUNT_TOKEN_RE = r"-?\s*(?:\d{1,3}(?:[ .]\d{3})*|\d+),\d{2}"
CURRENCY_TOKEN_RE = r"(?:K\w|[A-Z]{3})"
HEADER_AMOUNT_RE = re.compile(
    rf"(?<!\d)(?P<amount>{AMOUNT_TOKEN_RE})\s+(?P<currency>{CURRENCY_TOKEN_RE})\s*$"
)
HEADER_RE = re.compile(
    rf"^\s*{HEADER_DATE_RE}.*?(?<!\d){AMOUNT_TOKEN_RE}\s+{CURRENCY_TOKEN_RE}\s*$"
)
PAGE_RE = re.compile(r"^\d+/\d+$")

FX_DATE_AMOUNT_RE = re.compile(
    r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s+(?P<amount>\d[\d\s\.]*,\d{2})\s+(?P<currency>[A-Z]{3})\b"
)
FX_AMOUNT_ONLY_RE = re.compile(
    r"^\s*(?P<amount>\d[\d\s\.]*,\d{2})\s+(?P<currency>[A-Z]{3})\b"
)
FX_RATE_RE = re.compile(
    r"^\s*1\s+(?P<currency>[A-Z]{3})\s*=\s*(?P<rate>\d[\d\s]*,\d+)\s*K\w\b"
)

CARD_MASK_RE = re.compile(r"\b\d{4}\s\d{2}\*{2}\s\*{4}\s\d{4}\b")
CARD_MASK_X_RE = re.compile(r"\b\d{6}X{4,6}\d{4}\b", re.IGNORECASE)
CARD_MASK_STAR_RE = re.compile(r"\b\d{6}\*{4,6}\d{4}\b")
ACCOUNT_RE = re.compile(r"\b\d{1,10}-\d{1,10}/\d{4}\b")
ACCOUNT_RE2 = re.compile(r"\b\d{1,10}/\d{4}\b")


COLUMNS = [
    "Datum",
    "Popis_hlavicka",
    "Protistrana",
    "Protiucet",
    "Karta",
    "Castka_CZK",
    "Castka_raw",
    "Datum_provedeni",
    "Kod_transakce",
    "Typ_transakce",
    "VS",
    "SS",
    "KS",
    "Zprava",
    "ATM_ID",
    "FX_mena",
    "FX_kurz",
    "FX_kurz_mena",
    "Doplnek",
    "Blok_text",
]


IGNORE_SUBSTRINGS = [
    "vypis z uctu",
    "datum vypisu",
    "informace o uctu",
    "zustatky",
    "pocatecni zustatek",
    "konecny zustatek",
    "komercni banka, a. s.",
    "zapsana v obchodnim rejstriku",
    "trvaly pobyt",
    "cislo uctu",
    "iban",
    "hlavni mena",
    "typ uctu",
    "transakce",
]


CONTINUATION_TRIGGERS = {
    "na",
    "pres",
    "za",
    "pro",
    "do",
    "od",
    "v",
    "ve",
    "s",
    "z",
    "extra",
    "vyrovnavaci",
}


def normalize_text(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if ord(c) < 128
    ).lower()


def is_label_line(line: str) -> bool:
    n = normalize_text(line)
    return "datum proved" in n and "transakce" in n


def is_message_line(line: str) -> bool:
    return normalize_text(line).startswith("zpr")


def is_ignored_line(line: str) -> bool:
    if not line or not line.strip():
        return True
    if PAGE_RE.match(line.strip()):
        return True
    n = normalize_text(line)
    return any(s in n for s in IGNORE_SUBSTRINGS)


def append_field(tx: dict, key: str, value: str) -> None:
    if not value:
        return
    if tx.get(key):
        tx[key] = f"{tx[key]} | {value}"
    else:
        tx[key] = value


def cz_amount_to_float(s: str):
    s = s.strip()
    negative = s.startswith("-")
    s = s.replace("-", "").strip()
    s = s.replace(" ", "").replace("\u00a0", "")
    if "," in s:
        s = s.replace(".", "")
    s = s.replace(",", ".")
    try:
        value = float(s)
    except Exception:
        return None
    return -value if negative else value


def normalize_currency_token(token: str):
    if not token:
        return None
    token = token.strip().upper()
    if token.startswith("K"):
        return "CZK"
    return token


def looks_like_code_fragment(token: str) -> bool:
    token = token.strip()
    if len(token) < 8:
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", token):
        return False
    return bool(re.search(r"\d", token))


def parse_header_line(line: str):
    m_date = DATE_RE.match(line)
    if not m_date:
        return None

    m_amount = HEADER_AMOUNT_RE.search(line)
    amount_text = m_amount.group("amount").strip() if m_amount else None
    amount_value = cz_amount_to_float(amount_text) if amount_text else None
    header_currency = (
        normalize_currency_token(m_amount.group("currency")) if m_amount else None
    )

    header_without_amount = line
    if m_amount:
        header_without_amount = line[: m_amount.start()]

    header_without_amount = header_without_amount[m_date.end() :].strip()
    card_network = None
    if re.search(r"\bVISA\b", header_without_amount, re.IGNORECASE):
        card_network = "VISA"
    elif re.search(r"\bMASTERCARD\b", header_without_amount, re.IGNORECASE):
        card_network = "MASTERCARD"

    card = None
    m_card = (
        CARD_MASK_RE.search(header_without_amount)
        or CARD_MASK_X_RE.search(header_without_amount)
        or CARD_MASK_STAR_RE.search(header_without_amount)
    )
    if m_card:
        card = m_card.group(0).strip()
        header_without_amount = header_without_amount.replace(card, " ")

    if card_network:
        header_without_amount = re.sub(
            r"\b" + card_network + r"\b",
            " ",
            header_without_amount,
            flags=re.IGNORECASE,
        )

    account = None
    m_acc = ACCOUNT_RE.search(header_without_amount) or ACCOUNT_RE2.search(
        header_without_amount
    )
    if m_acc:
        account = m_acc.group(0)
        header_without_amount = header_without_amount.replace(account, " ")

    counterparty = re.sub(r"\s{2,}", " ", header_without_amount).strip()
    popis_hlavicka = counterparty if counterparty else None

    return {
        "Datum": m_date.group(1).strip(),
        "Popis_hlavicka": popis_hlavicka,
        "Protistrana": counterparty,
        "Protiucet": account,
        "Karta": card,
        "Karta_sit": card_network,
        "Castka_raw": amount_text,
        "Castka_CZK": amount_value,
        "Mena_hlavicka": header_currency,
    }


def parse_detail_main_line(line: str):
    m_date = DATE_RE.match(line)
    if not m_date:
        return {}, None

    exec_date = m_date.group(1).strip()
    rest = line[m_date.end() :].strip()
    tokens = rest.split()
    if len(tokens) < 4:
        return {"Datum_provedeni": exec_date}, None

    code = tokens[0]
    vs, ss, ks = tokens[-3:]
    type_tokens = tokens[1:-3]
    ttype = " ".join(type_tokens).strip()

    def clean_symbol(val: str) -> str:
        return "" if val == "-" else val

    return {
        "Datum_provedeni": exec_date,
        "Kod_transakce": code,
        "Typ_transakce": ttype,
        "VS": clean_symbol(vs),
        "SS": clean_symbol(ss),
        "KS": clean_symbol(ks),
    }, ttype


def should_append_type(line: str, current_type: str) -> bool:
    if not line or not line.strip():
        return False
    if ":" in line:
        return False
    if " - " in line:
        return False
    if "/" in line:
        return False
    if "*" in line:
        return False

    n_line = normalize_text(line)
    if n_line.startswith("popis pro me") or n_line.startswith("id souvisejici"):
        return False

    n_type = normalize_text(current_type or "")
    if not n_type:
        return False

    last_word = n_type.split()[-1]
    if last_word in CONTINUATION_TRIGGERS:
        return True

    if (
        len(n_type.split()) == 1
        and len(line.split()) <= 2
        and not re.search(r"\d", line)
    ):
        return True

    return False


def parse_fx_line(line: str, tx: dict) -> bool:
    m_rate = FX_RATE_RE.match(line)
    if m_rate:
        if not tx.get("FX_kurz"):
            tx["FX_kurz"] = m_rate.group("rate").replace(" ", "")
        if not tx.get("FX_kurz_mena"):
            tx["FX_kurz_mena"] = m_rate.group("currency")
        return True

    m_fx = FX_DATE_AMOUNT_RE.search(line)
    if m_fx:
        if not tx.get("FX_datum"):
            tx["FX_datum"] = m_fx.group("date")
        if not tx.get("FX_castka"):
            tx["FX_castka"] = m_fx.group("amount").replace(" ", "")
        if not tx.get("FX_mena"):
            tx["FX_mena"] = m_fx.group("currency")
        prefix = line[: m_fx.start()].strip()
        if prefix and not tx.get("FX_info"):
            tx["FX_info"] = prefix
        return True

    m_amt = FX_AMOUNT_ONLY_RE.match(line)
    if m_amt:
        if not tx.get("FX_castka"):
            tx["FX_castka"] = m_amt.group("amount").replace(" ", "")
        if not tx.get("FX_mena"):
            tx["FX_mena"] = m_amt.group("currency")
        return True

    if "kurz" in line.lower():
        return True

    return False


def handle_misc_line(line: str, tx: dict, extra_lines: list) -> None:
    if is_ignored_line(line):
        return
    n = normalize_text(line)
    if n.startswith("popis pro me"):
        if not tx.get("Popis_pro_me"):
            tx["Popis_pro_me"] = line.split(":", 1)[-1].strip()
        return
    if n.startswith("id souvisejici platby"):
        if not tx.get("ID_souvisejici_platby"):
            tx["ID_souvisejici_platby"] = line.split(":", 1)[-1].strip()
        return
    if n.startswith("atm id"):
        if not tx.get("ATM_ID"):
            tx["ATM_ID"] = line.split(":", 1)[-1].strip()
        return

    extra_lines.append(line.strip())


def parse_detail_lines(lines: list, tx: dict):
    extra_lines = []
    idx = 0
    while idx < len(lines) and not DATE_RE.match(lines[idx]):
        line = lines[idx]
        if not parse_fx_line(line, tx):
            handle_misc_line(line, tx, extra_lines)
        idx += 1

    if idx >= len(lines):
        return idx, extra_lines

    main_line = lines[idx]
    detail_info, _ = parse_detail_main_line(main_line)
    tx.update(detail_info)
    idx += 1

    while idx < len(lines):
        line = lines[idx]
        if is_message_line(line) or parse_fx_line(line, tx):
            break
        if DATE_RE.match(line):
            break
        if is_ignored_line(line):
            idx += 1
            continue

        tokens = line.strip().split()
        if tokens and tx.get("Kod_transakce"):
            first = tokens[0]
            if looks_like_code_fragment(first):
                tx["Kod_transakce"] = (tx.get("Kod_transakce", "") + first).strip()
                if len(tokens) > 1:
                    extra_type = " ".join(tokens[1:]).strip()
                    if extra_type:
                        tx["Typ_transakce"] = (
                            tx.get("Typ_transakce", "") + " " + extra_type
                        ).strip()
                idx += 1
                continue

        if should_append_type(line, tx.get("Typ_transakce")):
            tx["Typ_transakce"] = (
                tx.get("Typ_transakce", "") + " " + line.strip()
            ).strip()
        else:
            handle_misc_line(line, tx, extra_lines)
        idx += 1

    return idx, extra_lines


def extract_transactions(pdf_path: str):
    transactions = []

    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                lines.append(line)

    blocks = []
    current = []
    for line in lines:
        if HEADER_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        tx = process_block(block)
        if tx:
            transactions.append(tx)

    return transactions


def process_block(block_lines: list):
    header = parse_header_line(block_lines[0])
    if not header:
        return None

    tx = {col: None for col in COLUMNS}
    tx.update(header)

    label_idx = next((i for i, l in enumerate(block_lines) if is_label_line(l)), None)
    if label_idx is None:
        if not tx.get("FX_mena"):
            tx["FX_mena"] = tx.get("Mena_hlavicka") or "CZK"
        tx["Blok_text"] = " | ".join(block_lines)
        tx.pop("Mena_hlavicka", None)
        return tx

    pre_lines = block_lines[1:label_idx]
    post_lines = block_lines[label_idx + 1 :]

    extra_lines = []
    for line in pre_lines:
        if parse_fx_line(line, tx):
            continue
        handle_misc_line(line, tx, extra_lines)

    idx_after, extra2 = parse_detail_lines(post_lines, tx)
    extra_lines.extend(extra2)

    for line in post_lines[idx_after:]:
        if is_ignored_line(line):
            continue
        if is_message_line(line):
            msg = line.split(":", 1)[-1].strip() if ":" in line else line.strip()
            append_field(tx, "Zprava", msg)
            continue
        if parse_fx_line(line, tx):
            continue
        handle_misc_line(line, tx, extra_lines)

    if extra_lines:
        tx["Doplnek"] = (
            " | ".join([l for l in extra_lines if l]) if extra_lines else None
        )

    if not tx.get("FX_mena"):
        tx["FX_mena"] = tx.get("Mena_hlavicka") or "CZK"

    tx["Blok_text"] = " | ".join(block_lines)
    tx.pop("Mena_hlavicka", None)
    return tx


def main():
    pdf_file = Path(PDF_PATH)
    if not pdf_file.is_file():
        raise FileNotFoundError(f"Soubor {pdf_file} neexistuje")

    transactions = extract_transactions(str(pdf_file))
    if not transactions:
        print("Nenasly se zadne transakce - mozna jiny format PDF.")
        return

    df = pd.DataFrame(transactions, columns=COLUMNS)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Hotovo. Ulozeno do {OUTPUT_CSV}")

    # If you want Excel output too:
    # df.to_excel("transakce.xlsx", index=False)


if __name__ == "__main__":
    main()

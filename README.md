# KB Statement PDF Extractor

Extracts transaction data from Komerční banka PDF statements and exports it to CSV.

## Features

- Parses transaction blocks from KB statement PDFs
- Extracts key fields like date, amount, type, message, account/card info, and FX data
- Exports structured data to `transakce.csv`
- Handles CZK and foreign currency transactions (e.g., EUR, USD)

## Requirements

- Python 3.9+
- `pdfplumber`
- `pandas`

Install dependencies:

```bash
pip install pdfplumber pandas

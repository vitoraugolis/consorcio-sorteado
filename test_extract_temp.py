import re, sys

def _parse_br_number(raw):
    raw = raw.strip()
    if "," in raw and "." in raw:
        return float(raw.replace(".", "").replace(",", "."))
    if "," in raw:
        return float(raw.replace(",", "."))
    if "." in raw:
        parts = raw.split(".")
        if len(parts[-1]) == 3:
            return float(raw.replace(".", ""))
        return float(raw)
    return float(raw)

def _extract_lead_value(mensagem):
    texto = mensagem.lower()
    m = re.search(r"r\$\s*([\d.,]+)", texto)
    if m:
        try: return _parse_br_number(m.group(1))
        except ValueError: pass
    m = re.search(r"\b(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)\b", texto)
    if m:
        try: return _parse_br_number(m.group(1))
        except ValueError: pass
    m = re.search(r"(\d[\d.,]*)\s*mil\b", texto)
    if m:
        try:
            base = float(m.group(1).replace(".", "").replace(",", "."))
            return base * 1000
        except ValueError: pass
    m = re.search(r"(\d[\d.,]*)\s*k\b", texto)
    if m:
        try:
            base = float(m.group(1).replace(".", "").replace(",", "."))
            return base * 1000
        except ValueError: pass
    m = re.search(r"\b(\d{4,})\b", texto)
    if m:
        return float(m.group(1))
    return 0.0

casos = [
    ("Recebi uma proposta de 31k",        31000),
    ("Me ofereceram 320mil",              320000),
    ("Me ofereceram 320",                 0),
    ("Recebi 320.000",                    320000),
    ("Me pagaram R$ 320.000,00",          320000),
    ("Proposta de 320 mil reais",         320000),
    ("Recebi uma proposta de 320 mil",    320000),
    ("Fui oferecido 95.000,00",           95000),
    ("Me deram 1.200.000",                1200000),
    ("50k",                               50000),
    ("Recebi 280 mil",                    280000),
    ("320 mil",                           320000),
    ("proposta de 320",                   0),
]

print(f"{'Mensagem':<45} {'Esperado':>10} {'Extraído':>10} {'OK':>5}")
print("-"*75)
for msg, esp in casos:
    r = _extract_lead_value(msg)
    ok = "OK" if r == esp else "FAIL"
    print(f"{msg:<45} {esp:>10.0f} {r:>10.0f} {ok:>5}")

"""ISO-4217 currencies offered when configuring branches and entering expense items."""

# Every active ISO-4217 currency: (code, name, symbol). Frequently used ones are listed first.
POPULAR = ["AED", "INR", "USD", "EUR", "GBP", "SAR", "QAR", "OMR", "KWD", "BHD", "SGD"]

_ALL = [
    ('AED', 'UAE Dirham', 'د.إ'),
    ('AFN', 'Afghan Afghani', '؋'),
    ('ALL', 'Albanian Lek', 'L'),
    ('AMD', 'Armenian Dram', '֏'),
    ('ANG', 'Netherlands Antillean Guilder', 'ƒ'),
    ('AOA', 'Angolan Kwanza', 'Kz'),
    ('ARS', 'Argentine Peso', '$'),
    ('AUD', 'Australian Dollar', 'A$'),
    ('AWG', 'Aruban Florin', 'ƒ'),
    ('AZN', 'Azerbaijani Manat', '₼'),
    ('BAM', 'Bosnia-Herzegovina Convertible Mark', 'KM'),
    ('BBD', 'Barbadian Dollar', '$'),
    ('BDT', 'Bangladeshi Taka', '৳'),
    ('BGN', 'Bulgarian Lev', 'лв'),
    ('BHD', 'Bahraini Dinar', 'BD'),
    ('BIF', 'Burundian Franc', 'FBu'),
    ('BMD', 'Bermudian Dollar', '$'),
    ('BND', 'Brunei Dollar', 'B$'),
    ('BOB', 'Bolivian Boliviano', 'Bs'),
    ('BRL', 'Brazilian Real', 'R$'),
    ('BSD', 'Bahamian Dollar', '$'),
    ('BTN', 'Bhutanese Ngultrum', 'Nu.'),
    ('BWP', 'Botswana Pula', 'P'),
    ('BYN', 'Belarusian Ruble', 'Br'),
    ('BZD', 'Belize Dollar', 'BZ$'),
    ('CAD', 'Canadian Dollar', 'C$'),
    ('CDF', 'Congolese Franc', 'FC'),
    ('CHF', 'Swiss Franc', 'CHF'),
    ('CLP', 'Chilean Peso', '$'),
    ('CNY', 'Chinese Yuan', '¥'),
    ('COP', 'Colombian Peso', '$'),
    ('CRC', 'Costa Rican Colón', '₡'),
    ('CUP', 'Cuban Peso', '$'),
    ('CVE', 'Cape Verdean Escudo', '$'),
    ('CZK', 'Czech Koruna', 'Kč'),
    ('DJF', 'Djiboutian Franc', 'Fdj'),
    ('DKK', 'Danish Krone', 'kr'),
    ('DOP', 'Dominican Peso', 'RD$'),
    ('DZD', 'Algerian Dinar', 'DA'),
    ('EGP', 'Egyptian Pound', 'E£'),
    ('ERN', 'Eritrean Nakfa', 'Nfk'),
    ('ETB', 'Ethiopian Birr', 'Br'),
    ('EUR', 'Euro', '€'),
    ('FJD', 'Fijian Dollar', 'FJ$'),
    ('FKP', 'Falkland Islands Pound', '£'),
    ('GBP', 'British Pound', '£'),
    ('GEL', 'Georgian Lari', '₾'),
    ('GHS', 'Ghanaian Cedi', '₵'),
    ('GIP', 'Gibraltar Pound', '£'),
    ('GMD', 'Gambian Dalasi', 'D'),
    ('GNF', 'Guinean Franc', 'FG'),
    ('GTQ', 'Guatemalan Quetzal', 'Q'),
    ('GYD', 'Guyanese Dollar', '$'),
    ('HKD', 'Hong Kong Dollar', 'HK$'),
    ('HNL', 'Honduran Lempira', 'L'),
    ('HTG', 'Haitian Gourde', 'G'),
    ('HUF', 'Hungarian Forint', 'Ft'),
    ('IDR', 'Indonesian Rupiah', 'Rp'),
    ('ILS', 'Israeli New Shekel', '₪'),
    ('INR', 'Indian Rupee', '₹'),
    ('IQD', 'Iraqi Dinar', 'ع.د'),
    ('IRR', 'Iranian Rial', '﷼'),
    ('ISK', 'Icelandic Króna', 'kr'),
    ('JMD', 'Jamaican Dollar', 'J$'),
    ('JOD', 'Jordanian Dinar', 'JD'),
    ('JPY', 'Japanese Yen', '¥'),
    ('KES', 'Kenyan Shilling', 'KSh'),
    ('KGS', 'Kyrgyzstani Som', 'с'),
    ('KHR', 'Cambodian Riel', '៛'),
    ('KMF', 'Comorian Franc', 'CF'),
    ('KPW', 'North Korean Won', '₩'),
    ('KRW', 'South Korean Won', '₩'),
    ('KWD', 'Kuwaiti Dinar', 'KD'),
    ('KYD', 'Cayman Islands Dollar', '$'),
    ('KZT', 'Kazakhstani Tenge', '₸'),
    ('LAK', 'Lao Kip', '₭'),
    ('LBP', 'Lebanese Pound', 'L£'),
    ('LKR', 'Sri Lankan Rupee', 'Rs'),
    ('LRD', 'Liberian Dollar', '$'),
    ('LSL', 'Lesotho Loti', 'L'),
    ('LYD', 'Libyan Dinar', 'LD'),
    ('MAD', 'Moroccan Dirham', 'DH'),
    ('MDL', 'Moldovan Leu', 'L'),
    ('MGA', 'Malagasy Ariary', 'Ar'),
    ('MKD', 'Macedonian Denar', 'ден'),
    ('MMK', 'Myanmar Kyat', 'K'),
    ('MNT', 'Mongolian Tögrög', '₮'),
    ('MOP', 'Macanese Pataca', 'MOP$'),
    ('MRU', 'Mauritanian Ouguiya', 'UM'),
    ('MUR', 'Mauritian Rupee', 'Rs'),
    ('MVR', 'Maldivian Rufiyaa', 'Rf'),
    ('MWK', 'Malawian Kwacha', 'MK'),
    ('MXN', 'Mexican Peso', 'MX$'),
    ('MYR', 'Malaysian Ringgit', 'RM'),
    ('MZN', 'Mozambican Metical', 'MT'),
    ('NAD', 'Namibian Dollar', 'N$'),
    ('NGN', 'Nigerian Naira', '₦'),
    ('NIO', 'Nicaraguan Córdoba', 'C$'),
    ('NOK', 'Norwegian Krone', 'kr'),
    ('NPR', 'Nepalese Rupee', 'Rs'),
    ('NZD', 'New Zealand Dollar', 'NZ$'),
    ('OMR', 'Omani Rial', 'OMR'),
    ('PAB', 'Panamanian Balboa', 'B/.'),
    ('PEN', 'Peruvian Sol', 'S/'),
    ('PGK', 'Papua New Guinean Kina', 'K'),
    ('PHP', 'Philippine Peso', '₱'),
    ('PKR', 'Pakistani Rupee', 'Rs'),
    ('PLN', 'Polish Złoty', 'zł'),
    ('PYG', 'Paraguayan Guaraní', '₲'),
    ('QAR', 'Qatari Riyal', 'QR'),
    ('RON', 'Romanian Leu', 'lei'),
    ('RSD', 'Serbian Dinar', 'дин'),
    ('RUB', 'Russian Ruble', '₽'),
    ('RWF', 'Rwandan Franc', 'FRw'),
    ('SAR', 'Saudi Riyal', 'SR'),
    ('SBD', 'Solomon Islands Dollar', 'SI$'),
    ('SCR', 'Seychellois Rupee', 'SR'),
    ('SDG', 'Sudanese Pound', '£'),
    ('SEK', 'Swedish Krona', 'kr'),
    ('SGD', 'Singapore Dollar', 'S$'),
    ('SHP', 'Saint Helena Pound', '£'),
    ('SLE', 'Sierra Leonean Leone', 'Le'),
    ('SOS', 'Somali Shilling', 'Sh'),
    ('SRD', 'Surinamese Dollar', '$'),
    ('SSP', 'South Sudanese Pound', '£'),
    ('STN', 'São Tomé and Príncipe Dobra', 'Db'),
    ('SVC', 'Salvadoran Colón', '₡'),
    ('SYP', 'Syrian Pound', '£S'),
    ('SZL', 'Swazi Lilangeni', 'E'),
    ('THB', 'Thai Baht', '฿'),
    ('TJS', 'Tajikistani Somoni', 'SM'),
    ('TMT', 'Turkmenistani Manat', 'm'),
    ('TND', 'Tunisian Dinar', 'DT'),
    ('TOP', 'Tongan Paʻanga', 'T$'),
    ('TRY', 'Turkish Lira', '₺'),
    ('TTD', 'Trinidad and Tobago Dollar', 'TT$'),
    ('TWD', 'New Taiwan Dollar', 'NT$'),
    ('TZS', 'Tanzanian Shilling', 'TSh'),
    ('UAH', 'Ukrainian Hryvnia', '₴'),
    ('UGX', 'Ugandan Shilling', 'USh'),
    ('USD', 'US Dollar', '$'),
    ('UYU', 'Uruguayan Peso', '$U'),
    ('UZS', 'Uzbekistani Som', 'soʻm'),
    ('VES', 'Venezuelan Bolívar', 'Bs.S'),
    ('VND', 'Vietnamese Dong', '₫'),
    ('VUV', 'Vanuatu Vatu', 'VT'),
    ('WST', 'Samoan Tala', 'WS$'),
    ('XAF', 'Central African CFA Franc', 'FCFA'),
    ('XCD', 'East Caribbean Dollar', 'EC$'),
    ('XOF', 'West African CFA Franc', 'CFA'),
    ('XPF', 'CFP Franc', '₣'),
    ('YER', 'Yemeni Rial', '﷼'),
    ('ZAR', 'South African Rand', 'R'),
    ('ZMW', 'Zambian Kwacha', 'ZK'),
    ('ZWL', 'Zimbabwean Dollar', 'Z$'),
]
_ALL_BY_CODE = {c[0]: c for c in _ALL}
CURRENCIES = [_ALL_BY_CODE[c] for c in POPULAR] + [c for c in _ALL if c[0] not in POPULAR]

CURRENCY_CODES = {c[0] for c in CURRENCIES}
CURRENCY_CHOICES = [(code, f"{code} — {name}") for code, name, _ in CURRENCIES]
_BY_CODE = {c[0]: c for c in CURRENCIES}


def currency_name(code):
    row = _BY_CODE.get((code or "").upper())
    return row[1] if row else code


def currency_symbol(code):
    row = _BY_CODE.get((code or "").upper())
    return row[2] if row else code


def currency_json(code):
    return {"code": code, "name": currency_name(code), "symbol": currency_symbol(code)}


def all_currencies_json():
    return [currency_json(code) for code, _, _ in CURRENCIES]


def normalize_code(value):
    """Upper-cased 3-letter code, or "" when not a known currency."""
    code = (value or "").strip().upper()
    return code if code in CURRENCY_CODES else ""


def parse_code_list(value):
    """Accepts a list or a comma / space separated string; returns unique valid codes, in order."""
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = str(value or "").replace(";", ",").replace(" ", ",").split(",")
    out = []
    for item in raw:
        code = normalize_code(item)
        if code and code not in out:
            out.append(code)
    return out


_PLACE_CURRENCY = {
    "uae": "AED", "u.a.e": "AED", "u.a.e.": "AED", "united arab emirates": "AED",
    "dubai": "AED", "abu dhabi": "AED", "sharjah": "AED", "ajman": "AED",
    "india": "INR", "bharat": "INR", "kochi": "INR", "cochin": "INR", "kerala": "INR",
    "mumbai": "INR", "chennai": "INR", "bangalore": "INR", "bengaluru": "INR", "delhi": "INR", "new delhi": "INR",
    "singapore": "SGD", "saudi arabia": "SAR", "ksa": "SAR", "riyadh": "SAR", "jeddah": "SAR",
    "qatar": "QAR", "doha": "QAR", "oman": "OMR", "muscat": "OMR", "kuwait": "KWD", "bahrain": "BHD",
    "manama": "BHD", "united kingdom": "GBP", "uk": "GBP", "england": "GBP", "london": "GBP",
    "united states": "USD", "usa": "USD", "us": "USD", "malaysia": "MYR", "sri lanka": "LKR",
    "egypt": "EGP", "germany": "EUR", "france": "EUR", "netherlands": "EUR", "spain": "EUR", "italy": "EUR",
    "australia": "AUD", "canada": "CAD", "japan": "JPY", "china": "CNY", "hong kong": "HKD",
}


def guess_currency(*places):
    """AED / INR (etc.) inferred from a country or city name, or None."""
    for place in places:
        code = _PLACE_CURRENCY.get((place or "").strip().lower())
        if code:
            return code
    return None


def default_currency_for_country(country, fallback="USD", city=""):
    return guess_currency(country, city) or normalize_code(fallback) or "USD"

#!/usr/bin/env python3
"""
Plaza / NewNewNew housing watcher -> Telegram notifications.

Install:
    pip install requests python-dotenv playwright
    playwright install chromium

Run:
    python plaza_telegram_bot.py
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://plaza.newnewnew.space"
DEFAULT_LIST_URL = "https://plaza.newnewnew.space/aanbod/wonen#?gesorteerd-op=publicatiedatum-"
DETAIL_PATH_RE = re.compile(r"/aanbod/huurwoningen/details/\d+-[^\"'\s<>)]+", re.I)
LISTING_ID_RE = re.compile(r"/details/(\d+)-", re.I)
POSTCODE_RE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b", re.I)
DATE_RE = re.compile(r"\b\d{2}[-/]\d{2}[-/]\d{4}\b")
M2_RE = re.compile(r"\b\d+(?:[,.]\d+)?\s*m²\b", re.I)

CITY_WORDS = [
    "Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "Maastricht", "Breda",
    "Rijswijk", "Bochum", "Geldrop", "Amersfoort", "Deventer", "Groot-Ammers",
    "Enschede", "Delft", "Arnhem", "Groningen", "Leiden", "Den Haag", "Tilburg",
]

SECTION_WORDS = {
    "over de woning", "about the house", "kenmerken", "features", "afbeeldingen", "images",
    "woonruimtes", "living spaces", "omschrijving", "description", "beschikbaarheid",
    "availability", "kosten", "costs", "locatie", "location", "reageren", "reply",
    "deze woning wordt aangeboden door", "this property is offered by", "voorwaarden",
    "terms and conditions", "wat hebben wij van je nodig", "what information do we need from you",
}

FIELD_LABELS = {
    "doelgroep", "target group", "woningtype", "house type", "woningsoort", "housing type",
    "verdieping", "floor", "bouwjaar", "year of construction", "zonnepanelen", "solar panels",
    "oppervlakte woning", "living area", "oppervlakte woonkamer", "living room surface area",
    "aantal slaapkamers", "number of bedrooms", "slaapkamers", "bedrooms", "tuin", "garden",
    "balkon", "balcony", "beschikbaar per", "available per", "leeftijd", "age",
    "huishoudgrootte", "household size", "huishouden", "household", "kale huur", "basic rent",
    "servicekosten", "service charges", "stoffering", "upholstery", "meubilering", "furniture",
    "bijkomende kosten", "additional costs", "totale huurprijs", "total rental price",
    "eenmalige kosten", "one-time costs", "waarborgsom", "deposit", "borg",
}


@dataclass
class ListingDetails:
    listing_id: str
    url: str
    title: str = ""
    city: str = ""
    address: str = ""
    postcode_city: str = ""

    basic_rent: str = ""
    total_rent: str = ""
    service_charges: str = ""
    upholstery_furniture: str = ""
    additional_costs: str = ""
    one_time_costs: str = ""
    deposit: str = ""

    available_per: str = ""
    target_group: str = ""
    house_type: str = ""
    housing_type: str = ""
    floor: str = ""
    year_of_construction: str = ""
    solar_panels: str = ""
    living_area: str = ""
    living_room_area: str = ""
    bedrooms: str = ""
    balcony: str = ""
    garden: str = ""

    age_condition: str = ""
    household_condition: str = ""
    description: str = ""
    raw_text: str = ""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        print(f"Invalid integer for {name}: {value!r}. Using {default}.", file=sys.stderr)
        return default


def normalize_space(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def compact_lines(text: str) -> list[str]:
    lines = [normalize_space(line) for line in text.replace("\r", "").splitlines()]
    return [line for line in lines if line]


def keyify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def is_section_or_label(value: str) -> bool:
    value_clean = normalize_space(value).casefold().strip(" :")
    value_key = keyify(value_clean)

    if not value_clean:
        return True

    if value_clean in SECTION_WORDS:
        return True

    for label in FIELD_LABELS:
        if value_key == keyify(label):
            return True

    return False


def clean_money(value: str) -> str:
    """
    Extract a euro amount and return it normalized with € prefix.
    """
    if not value:
        return ""

    m = re.search(r"€\s*\d[\d\s.,]*\d|€\s*\d", value)
    if m:
        return normalize_space(m.group(0))

    # Fallback: amount without euro, used only after a known cost label.
    m = re.search(r"\b\d{1,3}(?:[.\s]\d{3})*(?:[,.]\d{2})\b|\b\d+(?:[,.]\d{2})\b", value)
    if m:
        return "€ " + normalize_space(m.group(0))

    return ""


def money_after_label(lines: list[str], aliases: Iterable[str], lookahead_lines: int = 7) -> str:
    """
    Find the first money value after a specific label.

    Important: this does NOT search the whole surrounding window, because that caused
    old bugs where € 932,00 was returned for every cost row.
    """
    aliases = list(aliases)

    for i, line in enumerate(lines):
        line_clean = normalize_space(line)
        line_key = keyify(line_clean)

        for alias in aliases:
            alias_key = keyify(alias)
            if not alias_key:
                continue

            # Case 1: label and value on the same line.
            if line_key.startswith(alias_key):
                money = clean_money(line_clean)
                if money:
                    return money

                # Case 2: label line, value in following lines.
                for j in range(i + 1, min(i + 1 + lookahead_lines, len(lines))):
                    candidate = lines[j]
                    if is_section_or_label(candidate):
                        continue
                    money = clean_money(candidate)
                    if money:
                        return money

            # Case 3: split labels, e.g. "Kale huur" + "prijs" + "€ 932,00".
            for n in range(2, 5):
                if i + n > len(lines):
                    continue

                label_part = " ".join(lines[i : i + n])
                label_part_key = keyify(label_part)

                if label_part_key.startswith(alias_key):
                    money = clean_money(label_part)
                    if money:
                        return money

                    for j in range(i + n, min(i + n + lookahead_lines, len(lines))):
                        candidate = lines[j]
                        if is_section_or_label(candidate):
                            continue
                        money = clean_money(candidate)
                        if money:
                            return money

    return ""


def first_match(patterns: Iterable[str], text: str, flags: int = re.I | re.S) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return normalize_space(match.group(1))
    return ""


def strip_label_prefix(value: str, aliases: Iterable[str]) -> str:
    raw = normalize_space(value).strip(" :")
    raw_key = keyify(raw)

    for alias in aliases:
        alias_key = keyify(alias)

        if alias_key and raw_key.startswith(alias_key) and raw_key != alias_key:
            pat = re.compile(r"^" + re.escape(alias) + r"\s*:?\s*", re.I)
            removed = normalize_space(pat.sub("", raw))

            if removed and removed != raw:
                return removed

            words = alias.split()
            tmp = raw

            for word in words:
                tmp = re.sub(r"^" + re.escape(word) + r"\s*", "", tmp, flags=re.I).strip(" :")

            if tmp and keyify(tmp) != raw_key:
                return normalize_space(tmp)

    return raw


def value_after_label(
    lines: list[str],
    aliases: Iterable[str],
    *,
    validator=None,
    lookahead_lines: int = 5,
) -> str:
    """
    Strict non-money label parser with optional validator.
    """
    aliases = list(aliases)

    for i, line in enumerate(lines):
        line_clean = normalize_space(line)
        line_key = keyify(line_clean)

        for alias in aliases:
            alias_key = keyify(alias)
            if not alias_key:
                continue

            # Same line: "Woningtype Studio".
            if line_key.startswith(alias_key) and line_key != alias_key:
                candidate = strip_label_prefix(line_clean, aliases)

                if candidate and not is_section_or_label(candidate):
                    if validator is None or validator(candidate):
                        return candidate

            # Exact label line.
            if line_key == alias_key:
                for j in range(i + 1, min(i + 1 + lookahead_lines, len(lines))):
                    candidate = normalize_space(lines[j]).strip(" :")

                    if not candidate or is_section_or_label(candidate):
                        continue

                    if validator is None or validator(candidate):
                        return candidate

            # Split label: "Aantal" + "slaapkamers".
            for n in range(2, 4):
                if i + n > len(lines):
                    continue

                joined = " ".join(lines[i : i + n])
                joined_key = keyify(joined)

                if joined_key == alias_key:
                    for j in range(i + n, min(i + n + lookahead_lines, len(lines))):
                        candidate = normalize_space(lines[j]).strip(" :")

                        if not candidate or is_section_or_label(candidate):
                            continue

                        if validator is None or validator(candidate):
                            return candidate

    return ""


def first_valid_date_after_label(lines: list[str], aliases: Iterable[str]) -> str:
    def valid_date(v: str) -> bool:
        return bool(DATE_RE.search(v))

    value = value_after_label(lines, aliases, validator=valid_date, lookahead_lines=8)
    match = DATE_RE.search(value)

    return match.group(0) if match else ""


def section_between(
    text: str,
    start_labels: Iterable[str],
    end_labels: Iterable[str],
    max_chars: int = 900,
) -> str:
    starts = "|".join(re.escape(s) for s in start_labels)
    ends = "|".join(re.escape(e) for e in end_labels)

    match = re.search(rf"(?:{starts})\s*(.*?)(?:\n\s*(?:{ends})\b|$)", text, re.I | re.S)

    if not match:
        return ""

    section = normalize_space(match.group(1))
    section = re.sub(r"\+\s*more$", "", section, flags=re.I).strip()

    if len(section) > max_chars:
        section = section[: max_chars - 1].rstrip() + "…"

    return section


def listing_id_from_url(url: str) -> Optional[str]:
    match = LISTING_ID_RE.search(url)
    return match.group(1) if match else None


def setup_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_listings (
            listing_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            first_seen_utc TEXT NOT NULL,
            details_json TEXT
        )
        """
    )
    con.commit()
    return con


def known_ids(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT listing_id FROM seen_listings")}


def mark_seen(con: sqlite3.Connection, details: ListingDetails) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO seen_listings
            (listing_id, url, title, first_seen_utc, details_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            details.listing_id,
            details.url,
            details.title,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(asdict(details), ensure_ascii=False),
        ),
    )
    con.commit()


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    disable_web_page_preview: bool = False,
) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }

    response = requests.post(endpoint, json=payload, timeout=30)

    if not response.ok:
        raise RuntimeError(f"Telegram sendMessage failed: {response.status_code} {response.text}")


def format_field(label: str, value: str) -> str:
    value = normalize_space(value)

    if not value or value == "€" or is_section_or_label(value):
        return ""

    return f"<b>{html.escape(label)}:</b> {html.escape(value)}\n"


def format_listing_message(d: ListingDetails) -> str:
    title = d.title or d.address or f"Plaza listing {d.listing_id}"
    safe_url = html.escape(d.url, quote=True)

    parts = [
        "🏠 <b>New Plaza publication</b>\n",
        f"<b>{html.escape(title)}</b>\n",
    ]

    parts.append(format_field("City", d.city))
    parts.append(format_field("Address", d.address))
    parts.append(format_field("Postcode/city", d.postcode_city))
    parts.append(format_field("Available per", d.available_per))
    parts.append("\n")

    parts.append(format_field("Basic rent", d.basic_rent))
    parts.append(format_field("Total rent", d.total_rent))
    parts.append(format_field("Service charges", d.service_charges))
    parts.append(format_field("Upholstery/furniture", d.upholstery_furniture))
    parts.append(format_field("Additional costs", d.additional_costs))
    parts.append(format_field("One-time costs", d.one_time_costs))
    parts.append(format_field("Deposit", d.deposit))
    parts.append("\n")

    parts.append(format_field("Target group", d.target_group))
    parts.append(format_field("House type", d.house_type))
    parts.append(format_field("Housing type", d.housing_type))
    parts.append(format_field("Floor", d.floor))
    parts.append(format_field("Year of construction", d.year_of_construction))
    parts.append(format_field("Solar panels", d.solar_panels))
    parts.append(format_field("Living area", d.living_area))
    parts.append(format_field("Living room area", d.living_room_area))
    parts.append(format_field("Bedrooms", d.bedrooms))
    parts.append(format_field("Balcony", d.balcony))
    parts.append(format_field("Garden", d.garden))
    parts.append("\n")

    parts.append(format_field("Age condition", d.age_condition))
    parts.append(format_field("Household condition", d.household_condition))

    if d.description:
        parts.append(f"\n<b>Description:</b> {html.escape(d.description)}\n")

    parts.append(f"\n<a href=\"{safe_url}\">Open listing</a>")

    msg = "".join(parts)

    if len(msg) > 3900:
        msg = msg[:3800].rsplit("\n", 1)[0] + f"\n\n<a href=\"{safe_url}\">Open listing</a>"

    return msg


def auto_scroll(page) -> None:
    previous_height = 0

    for _ in range(10):
        current_height = page.evaluate("document.body.scrollHeight")
        page.mouse.wheel(0, 3500)
        page.wait_for_timeout(700)

        if current_height == previous_height:
            break

        previous_height = current_height

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def collect_listing_urls(page, list_url: str) -> list[str]:
    page.goto(list_url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(3000)
    auto_scroll(page)

    urls: set[str] = set()

    try:
        hrefs = page.locator('a[href*="/aanbod/huurwoningen/details/"]').evaluate_all(
            "els => els.map(a => a.href || a.getAttribute('href')).filter(Boolean)"
        )

        for href in hrefs:
            urls.add(urljoin(BASE_URL, href))

    except Exception as exc:
        print(f"Could not read listing anchors: {exc!r}", file=sys.stderr, flush=True)

    try:
        rendered_html = page.content()

        for path in DETAIL_PATH_RE.findall(rendered_html):
            urls.add(urljoin(BASE_URL, path))

    except Exception as exc:
        print(f"Could not scan rendered HTML: {exc!r}", file=sys.stderr, flush=True)

    try:
        dom_strings = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('*'))
                .flatMap(el => [
                    el.href,
                    el.getAttribute && el.getAttribute('href'),
                    el.getAttribute && el.getAttribute('data-href'),
                    el.getAttribute && el.getAttribute('to'),
                    el.outerHTML
                ])
                .filter(Boolean)
            """
        )

        for item in dom_strings:
            for path in DETAIL_PATH_RE.findall(str(item)):
                urls.add(urljoin(BASE_URL, path))

    except Exception as exc:
        print(f"Could not scan DOM attributes: {exc!r}", file=sys.stderr, flush=True)

    def sort_key(url: str) -> int:
        ident = listing_id_from_url(url)
        return int(ident or 0)

    return sorted(urls, key=sort_key, reverse=True)


def extract_title_from_page(page, lines: list[str], url: str) -> str:
    for selector in ["h1", "h2"]:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)

            for idx in range(count):
                title = normalize_space(locator.nth(idx).inner_text(timeout=1500))

                if title and not re.search(
                    r"aanbod|offer|vind jouw ruimte|where are you looking",
                    title,
                    re.I,
                ):
                    return title

        except Exception:
            pass

    for line in lines[:35]:
        if POSTCODE_RE.search(line):
            continue

        if any(city.casefold() in line.casefold() for city in CITY_WORDS):
            if not re.search(r"total|totaal|rental|huurprijs|p/m|service|kosten", line, re.I):
                return line

    slug = url.rsplit("/", 1)[-1]
    return re.sub(r"^\d+-", "", slug).replace("-", " ").title()


def extract_city(blob: str, title: str, postcode_city: str) -> str:
    text = "\n".join([title, postcode_city, blob])
    city_pattern = r"\b(" + "|".join(map(re.escape, CITY_WORDS)) + r")\b"
    city = first_match([city_pattern], text)

    return city.title() if city else ""


def extract_postcode_city(lines: list[str]) -> str:
    for line in lines[:100]:
        if POSTCODE_RE.search(line):
            return line

    return ""


def extract_address(lines: list[str], title: str, city: str, postcode_city: str) -> str:
    if title and not POSTCODE_RE.search(title) and not is_section_or_label(title):
        return title

    if city:
        for line in lines[:80]:
            if POSTCODE_RE.search(line):
                continue

            if city.casefold() in line.casefold() and not re.search(
                r"total|totaal|rental|huurprijs|service|deposit|waarborgsom|p/m",
                line,
                re.I,
            ):
                return line

    return title or postcode_city


def dump_debug_text(listing_id: str, lines: list[str]) -> None:
    if not env_bool("DEBUG_DUMP_TEXT", False):
        return

    filename = f"debug_plaza_{listing_id}.txt"

    with open(filename, "w", encoding="utf-8") as f:
        for idx, line in enumerate(lines):
            f.write(f"{idx:03d}: {line}\n")

    print(f"Debug text written to {filename}", flush=True)


def extract_details(page, url: str) -> ListingDetails:
    listing_id = listing_id_from_url(url) or url

    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(2500)

    try:
        text = page.locator("main").inner_text(timeout=4000)
    except PlaywrightTimeoutError:
        text = page.locator("body").inner_text(timeout=12000)

    lines = compact_lines(text)
    blob = "\n".join(lines)

    dump_debug_text(str(listing_id), lines)

    title = extract_title_from_page(page, lines, url)
    postcode_city = extract_postcode_city(lines)
    city = extract_city(blob, title, postcode_city)
    address = extract_address(lines, title, city, postcode_city)

    is_yes_no = lambda v: keyify(v) in {"ja", "nee", "yes", "no"}
    is_year = lambda v: bool(re.search(r"\b(?:19|20)\d{2}\b", v))
    is_area = lambda v: bool(M2_RE.search(v))
    is_floor = lambda v: bool(
        re.search(r"\b(?:\d+e|\d+(?:st|nd|rd|th)|begane grond|ground)\b.*\b(?:verdieping|floor)\b", v, re.I)
    )
    is_age = lambda v: bool(re.search(r"\b(?:minimaal|minimum|maximaal|maximum|jaar|years?|18|27)\b", v, re.I))
    is_household = lambda v: bool(re.search(r"\b(?:persoon|person|personen|people|huishoud)\b", v, re.I))
    is_bedrooms = lambda v: bool(re.search(r"\b(?:geen losse slaapkamer|no separate bedroom|\d+)\b", v, re.I))

    details = ListingDetails(
        listing_id=listing_id,
        url=url,
        title=title,
        city=city,
        address=address,
        postcode_city=postcode_city,

        basic_rent=money_after_label(lines, ["Kale huur", "Kale huurprijs", "Basic rent", "Net rent"]),
        total_rent=(
            first_match(
                [
                    r"(?:Totale huurprijs|Totaal huurprijs|Total rental price)\s*:?\s*(€\s*\d[\d\s.,]*\d)",
                ],
                blob,
            )
            or money_after_label(lines, ["Totale huurprijs", "Totaal huurprijs", "Total rental price"])
        ),
        service_charges=money_after_label(lines, ["Servicekosten", "Service kosten", "Service charges"]),
        upholstery_furniture=money_after_label(
            lines,
            [
                "Stoffering / meubilering",
                "Stoffering meubilering",
                "Stoffering",
                "Upholstery / furniture",
                "Upholstery furniture",
            ],
        ),
        additional_costs=money_after_label(lines, ["Bijkomende kosten", "Additional costs"]),
        one_time_costs=money_after_label(lines, ["Eenmalige kosten", "One-time costs", "One time costs"]),
        deposit=money_after_label(lines, ["Waarborgsom", "Deposit", "Borg"]),

        available_per=first_valid_date_after_label(lines, ["Beschikbaar per", "Available per"]),
        target_group=value_after_label(lines, ["Doelgroep", "Target group"]),
        house_type=value_after_label(lines, ["Woningtype", "House type"]),
        housing_type=value_after_label(lines, ["Woningsoort", "Housing type", "Type woonruimte"]),
        floor=value_after_label(lines, ["Verdieping", "Floor", "Etage"], validator=is_floor),
        year_of_construction=value_after_label(lines, ["Bouwjaar", "Year of construction"], validator=is_year),
        solar_panels=value_after_label(lines, ["Zonnepanelen", "Solar panels"], validator=is_yes_no),
        living_area=value_after_label(lines, ["Oppervlakte woning", "Woonoppervlakte", "Living area"], validator=is_area),
        living_room_area=value_after_label(
            lines,
            ["Oppervlakte woonkamer", "Living room surface area", "Living room area"],
            validator=is_area,
        ),
        bedrooms=value_after_label(
            lines,
            ["Aantal slaapkamers", "Number of bedrooms", "Slaapkamers", "Bedrooms"],
            validator=is_bedrooms,
        ),
        balcony=value_after_label(lines, ["Balkon", "Balcony"], validator=is_yes_no),
        garden=value_after_label(lines, ["Tuin", "Garden"], validator=is_yes_no),
        age_condition=value_after_label(lines, ["Leeftijd", "Age"], validator=is_age),
        household_condition=value_after_label(
            lines,
            ["Huishoudgrootte", "Household size", "Huishouden", "Household"],
            validator=is_household,
        ),
        description=section_between(
            blob,
            ["Omschrijving", "Description"],
            [
                "Beschikbaarheid",
                "Availability",
                "Deze woning wordt aangeboden door",
                "This property is offered by",
                "Locatie",
                "Location",
                "Kosten",
                "Costs",
            ],
        ),
        raw_text=blob[:5000],
    )

    if not details.available_per:
        match = DATE_RE.search(blob)
        details.available_per = match.group(0) if match else ""

    if not details.living_area:
        match = M2_RE.search(blob)
        details.living_area = match.group(0) if match else ""

    return details


def money_to_float(value: str) -> Optional[float]:
    if not value:
        return None

    money = clean_money(value)

    if not money:
        return None

    cleaned = re.sub(r"[^\d,.]", "", money)

    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    elif "," in cleaned:
        if len(cleaned.rsplit(",", 1)[-1]) == 2:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)

    except ValueError:
        return None


def passes_filters(d: ListingDetails) -> bool:
    city_filter = os.getenv("CITY_FILTER", "").strip().casefold()

    if city_filter:
        searchable = "\n".join([d.city, d.address, d.postcode_city, d.title, d.raw_text]).casefold()

        if city_filter not in searchable:
            return False

    max_total_rent_raw = os.getenv("MAX_TOTAL_RENT", "").strip()

    if max_total_rent_raw:
        try:
            max_total_rent = float(max_total_rent_raw.replace(",", "."))

        except ValueError:
            print(f"Invalid MAX_TOTAL_RENT={max_total_rent_raw!r}. Ignoring rent filter.", file=sys.stderr)
            return True

        rent_value = money_to_float(d.total_rent or d.basic_rent)

        if rent_value is not None and rent_value > max_total_rent:
            return False

    return True


def check_once(con: sqlite3.Connection, token: str, chat_id: str, list_url: str) -> tuple[int, int, int]:
    seen = known_ids(con)
    is_first_run = len(seen) == 0
    send_existing_on_first_run = env_bool("SEND_EXISTING_ON_FIRST_RUN", False)

    discovered = 0
    matched = 0
    sent = 0

    headless = env_bool("HEADLESS", True)
    slow_mo = env_int("SLOW_MO_MS", 0)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)

        try:
            context = browser.new_context(
                locale=os.getenv("BROWSER_LOCALE", "nl-NL"),
                viewport={"width": 1440, "height": 1400},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            urls = collect_listing_urls(page, list_url)

            print(f"Found {len(urls)} listing detail URL(s).", flush=True)

            if not urls:
                return 0, 0, 0

            for url in urls:
                ident = listing_id_from_url(url)

                if not ident or ident in seen:
                    continue

                try:
                    details = extract_details(page, url)

                except Exception as exc:
                    print(f"Could not extract details from {url}: {exc!r}", file=sys.stderr, flush=True)
                    continue

                discovered += 1
                mark_seen(con, details)
                seen.add(details.listing_id)

                if not passes_filters(details):
                    continue

                matched += 1

                if is_first_run and not send_existing_on_first_run:
                    continue

                send_telegram_message(token, chat_id, format_listing_message(details))
                sent += 1
                time.sleep(1.0)

        finally:
            browser.close()

    if is_first_run and discovered and not send_existing_on_first_run:
        send_telegram_message(
            token,
            chat_id,
            (
                "✅ Plaza watcher initialized. "
                f"I saved {discovered} current listing(s), of which {matched} matched your filters. "
                "From now on I will notify only future new matching publications."
            ),
            disable_web_page_preview=True,
        )

    return discovered, matched, sent


def main() -> int:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    list_url = os.getenv("LIST_URL", DEFAULT_LIST_URL).strip()
    db_path = os.getenv("DB_PATH", "plaza_seen.sqlite3").strip()
    interval = env_int("CHECK_INTERVAL_SECONDS", 60)

    if not token or token == "123456789:replace_me":
        print("Missing TELEGRAM_BOT_TOKEN. Put the real @BotFather token in .env.", file=sys.stderr)
        return 2

    if not chat_id:
        print("Missing TELEGRAM_CHAT_ID. Put your Telegram chat id in .env.", file=sys.stderr)
        return 2

    if interval < 30:
        print("CHECK_INTERVAL_SECONDS is too low. Using 30 seconds minimum.", file=sys.stderr)
        interval = 30

    con = setup_db(db_path)

    print(f"Watching: {list_url}", flush=True)
    print(f"Interval: {interval}s | DB: {db_path}", flush=True)
    print(
        "Filters: "
        f"CITY_FILTER={os.getenv('CITY_FILTER', '').strip()!r}, "
        f"MAX_TOTAL_RENT={os.getenv('MAX_TOTAL_RENT', '').strip()!r}",
        flush=True,
    )

    while True:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            discovered, matched, sent = check_once(con, token, chat_id, list_url)

            print(
                f"[{started}] discovered_new={discovered} matched_filters={matched} sent={sent}",
                flush=True,
            )

        except KeyboardInterrupt:
            print("Stopped.", flush=True)
            return 0

        except Exception as exc:
            print(f"[{started}] ERROR: {exc!r}", file=sys.stderr, flush=True)

            try:
                send_telegram_message(
                    token,
                    chat_id,
                    f"⚠️ Plaza watcher error: <code>{html.escape(repr(exc))}</code>",
                    disable_web_page_preview=True,
                )

            except Exception:
                pass

        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
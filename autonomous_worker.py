import html
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI
from rapidfuzz import fuzz
from requests.auth import HTTPBasicAuth


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# .env is convenient for local development, but normal environment
# variables are also supported. The repository should never contain
# the real .env file.
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)


def env_int(name, default, minimum=None):
    raw = os.getenv(name, str(default)).strip()

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}") from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")

    return value


def required_env(name):
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


WP_URL = required_env("WP_URL").rstrip("/")
WP_USER = required_env("WP_USER")
WP_PASSWORD = required_env("WP_APP_PASSWORD")
OPENAI_API_KEY = required_env("OPENAI_API_KEY")

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.6-luna",
).strip()

COUNTRY_NAME = os.getenv(
    "COUNTRY_NAME",
    "United States",
).strip()

COUNTRY_QID = os.getenv(
    "COUNTRY_QID",
    "Q30",
).strip()

WP_COUNTRY = os.getenv(
    "WP_COUNTRY",
    COUNTRY_NAME,
).strip()

NOMINATIM_COUNTRY_CODE = os.getenv(
    "NOMINATIM_COUNTRY_CODE",
    "us",
).strip().lower()

if not re.fullmatch(r"Q\d+", COUNTRY_QID):
    raise RuntimeError(
        f"COUNTRY_QID must look like Q30, got: {COUNTRY_QID!r}"
    )

if not re.fullmatch(r"[a-z]{2}", NOMINATIM_COUNTRY_CODE):
    raise RuntimeError(
        "NOMINATIM_COUNTRY_CODE must be a two-letter country code"
    )

BATCH_SIZE = env_int(
    "BATCH_SIZE",
    25,
    minimum=1,
)

# Safe default for a public/demo repository. Set to 0 explicitly when
# a reviewed configuration is ready to process the full source scope.
MAX_PEOPLE_PER_RUN = env_int(
    "MAX_PEOPLE_PER_RUN",
    10,
    minimum=0,
)

SLEEP_BETWEEN_BATCHES = env_int(
    "SLEEP_BETWEEN_BATCHES",
    2,
    minimum=0,
)

SPARQL_RETRY_SECONDS = env_int(
    "SPARQL_RETRY_SECONDS",
    60,
    minimum=1,
)

RAW_DB_FILE = os.getenv(
    "DB_FILE",
    "cemetery_agent_state.db",
).strip()

DB_FILE = Path(RAW_DB_FILE)
if not DB_FILE.is_absolute():
    DB_FILE = BASE_DIR / DB_FILE

SOURCE_KEY = os.getenv(
    "SOURCE_KEY",
    f"wikidata_{COUNTRY_QID.lower()}",
).strip()

SPARQL_ENDPOINTS = [
    (
        "QLever",
        "https://qlever.cs.uni-freiburg.de/api/wikidata",
    ),
    (
        "Wikidata",
        "https://query.wikidata.org/sparql",
    ),
]

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "CemeteryLink-Agent/1.0 (https://cemeterylink.com)",
).strip()

TERMINAL_STATUSES = {
    "CREATED",
    "UPDATED",
    "UNCHANGED",
    "DUPLICATE",
    "SKIPPED",
}

client = OpenAI(api_key=OPENAI_API_KEY)
db = None


# ============================================================
# EXCEPTIONS
# ============================================================


class InsufficientEvidenceError(RuntimeError):
    """The workflow deliberately abstained because location evidence was weak."""


# ============================================================
# DATABASE
# ============================================================


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_database():
    global db

    db = sqlite3.connect(DB_FILE)

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS cursors (
            source_key TEXT PRIMARY KEY,
            offset_value INTEGER NOT NULL DEFAULT 0,
            finished INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS processed (
            external_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            cemetery_id INTEGER,
            grave_id INTEGER,
            reason TEXT,
            processed_at TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS cemetery_research (
            cemetery_key TEXT PRIMARY KEY,
            description TEXT,
            sources_json TEXT,
            researched_at TEXT NOT NULL
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS osm_cache (
            search_key TEXT PRIMARY KEY,
            result_json TEXT,
            searched_at TEXT NOT NULL
        )
        """
    )

    db.commit()


def require_db():
    if db is None:
        raise RuntimeError("Database is not initialized")


def get_cursor():
    require_db()

    row = db.execute(
        """
        SELECT offset_value, finished
        FROM cursors
        WHERE source_key = ?
        """,
        (SOURCE_KEY,),
    ).fetchone()

    if not row:
        db.execute(
            """
            INSERT INTO cursors (
                source_key,
                offset_value,
                finished
            )
            VALUES (?, 0, 0)
            """,
            (SOURCE_KEY,),
        )
        db.commit()
        return 0, False

    return int(row[0]), bool(row[1])


def save_cursor(offset_value, finished=False):
    require_db()

    db.execute(
        """
        INSERT INTO cursors (
            source_key,
            offset_value,
            finished
        )
        VALUES (?, ?, ?)

        ON CONFLICT(source_key)
        DO UPDATE SET
            offset_value = excluded.offset_value,
            finished = excluded.finished
        """,
        (
            SOURCE_KEY,
            int(offset_value),
            1 if finished else 0,
        ),
    )

    db.commit()


def already_processed(external_id):
    """
    Only terminal outcomes suppress future processing.

    ERROR is intentionally retryable on a later run. This avoids losing a
    record forever because WordPress, OpenAI, Wikidata or another dependency
    had a temporary failure.
    """
    require_db()

    row = db.execute(
        """
        SELECT status
        FROM processed
        WHERE external_id = ?
        """,
        (external_id,),
    ).fetchone()

    if not row:
        return False

    return clean(row[0]).upper() in TERMINAL_STATUSES


def mark_processed(
    external_id,
    status,
    cemetery_id=None,
    grave_id=None,
    reason="",
):
    require_db()

    db.execute(
        """
        INSERT OR REPLACE INTO processed (
            external_id,
            status,
            cemetery_id,
            grave_id,
            reason,
            processed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            external_id,
            status,
            cemetery_id,
            grave_id,
            reason,
            utc_now_iso(),
        ),
    )

    db.commit()


# ============================================================
# GENERIC HELPERS
# ============================================================


def clean(value):
    if value is None:
        return ""

    value = html.unescape(str(value)).strip()

    if value.lower() in {
        "",
        "none",
        "null",
        "unknown",
        "nan",
    }:
        return ""

    return value


def qid_from_uri(uri):
    uri = clean(uri)

    if not uri:
        return ""

    return uri.rstrip("/").split("/")[-1]


def normalize_name(value):
    value = clean(value).lower()

    # CemeteryLink titles often look like:
    # Name - City - Region - Country
    value = re.sub(
        r"\s+[-–—]\s+.*$",
        "",
        value,
    )

    replacements = [
        "cemetery",
        "cemetary",
        "graveyard",
        "grave yard",
        "burial ground",
        "burial grounds",
        "churchyard",
    ]

    for word in replacements:
        value = value.replace(word, " ")

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return " ".join(value.split())


def value(row, key):
    return clean(
        row.get(key, {}).get("value", "")
    )


def parse_point(raw_value):
    raw_value = clean(raw_value)

    if not raw_value:
        return None

    match = re.search(
        r"Point\(([-0-9.]+)\s+([-0-9.]+)\)",
        raw_value,
    )

    if not match:
        return None

    longitude = float(match.group(1))
    latitude = float(match.group(2))

    return latitude, longitude


def distance_km(lat1, lon1, lat2, lon2):
    radius = 6371.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    return 2 * radius * math.asin(math.sqrt(a))


def split_name(full_name):
    parts = clean(full_name).split()

    if not parts:
        return "", "", ""

    if len(parts) == 1:
        return parts[0], "", ""

    return parts[0], parts[-1], ""


def first_non_empty(*values):
    for item in values:
        item = clean(item)

        if item:
            return item

    return ""


def compact_payload(payload):
    """Remove empty values so merge endpoints do not receive blank fields."""
    result = {}

    for key, item in payload.items():
        if item is None:
            continue

        if isinstance(item, str) and not item.strip():
            continue

        if isinstance(item, (list, tuple, dict)) and not item:
            continue

        result[key] = item

    return result


def dedupe_urls(urls):
    result = []
    seen = set()

    for raw_url in urls:
        url = clean(raw_url)

        if not url:
            continue

        if not re.match(r"^https?://", url, flags=re.I):
            continue

        if url in seen:
            continue

        seen.add(url)
        result.append(url)

    return result


def cemetery_context(candidate):
    return normalize_name(
        " ".join(
            [
                clean(candidate.get("city")),
                clean(candidate.get("region")),
                clean(candidate.get("title")),
            ]
        )
    )


# ============================================================
# WORDPRESS / CEMETERYLINK
# ============================================================


def wp_get(path, params=None):
    for attempt in range(1, 4):
        try:
            response = requests.get(
                WP_URL + path,
                params=params,
                auth=HTTPBasicAuth(
                    WP_USER,
                    WP_PASSWORD,
                ),
                headers={
                    "User-Agent": USER_AGENT,
                },
                timeout=60,
            )

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            print(
                f"WordPress GET error {attempt}/3:",
                exc,
            )
            time.sleep(attempt * 3)

    raise RuntimeError("WordPress GET failed after 3 attempts")


def wp_post(path, payload):
    for attempt in range(1, 4):
        try:
            response = requests.post(
                WP_URL + path,
                json=payload,
                auth=HTTPBasicAuth(
                    WP_USER,
                    WP_PASSWORD,
                ),
                headers={
                    "User-Agent": USER_AGENT,
                },
                timeout=60,
            )

            if response.ok:
                return response.json()

            print(
                "WordPress HTTP:",
                response.status_code,
            )
            print(response.text[:700])

        except Exception as exc:
            print(
                f"WordPress POST error {attempt}/3:",
                exc,
            )

        time.sleep(attempt * 3)

    raise RuntimeError("WordPress POST failed after 3 attempts")


def load_wp_cemeteries():
    print(
        "Loading CemeteryLink cemeteries:",
        COUNTRY_NAME,
    )

    results = []
    seen = set()
    page = 1

    while True:
        batch = wp_get(
            "/wp-json/cemetery-agent/v1/cemeteries",
            {
                "country": WP_COUNTRY,
                "limit": 50,
                "page": page,
            },
        )

        if not batch:
            break

        new_count = 0

        for cemetery in batch:
            cemetery_id = int(cemetery["id"])

            if cemetery_id in seen:
                continue

            seen.add(cemetery_id)
            cemetery["_normalized"] = normalize_name(
                cemetery.get("title", "")
            )
            results.append(cemetery)
            new_count += 1

        if new_count == 0 or len(batch) < 50:
            break

        page += 1

    print("Loaded:", len(results))
    return results


# ============================================================
# WIKIDATA DISCOVERY
# ============================================================


def wikidata_query(limit, offset):
    query = f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT
    ?person
    ?personLabel
    ?cemetery
    ?cemeteryLabel
    ?coord
    ?admin
    ?adminLabel

WHERE {{
    ?person
        wdt:P31 wd:Q5 ;
        wdt:P119 ?cemetery .

    ?cemetery
        wdt:P17 wd:{COUNTRY_QID} .

    ?person
        rdfs:label ?personLabel .

    FILTER(LANG(?personLabel) = "en")

    ?cemetery
        rdfs:label ?cemeteryLabel .

    FILTER(LANG(?cemeteryLabel) = "en")

    OPTIONAL {{
        ?cemetery wdt:P625 ?coord .
    }}

    OPTIONAL {{
        ?cemetery wdt:P131 ?admin .
        ?admin rdfs:label ?adminLabel .
        FILTER(LANG(?adminLabel) = "en")
    }}
}}

ORDER BY ?person
LIMIT {limit}
OFFSET {offset}
"""

    for endpoint_name, endpoint_url in SPARQL_ENDPOINTS:
        print("Trying SPARQL:", endpoint_name)

        for attempt in range(1, 3):
            try:
                params = {
                    "query": query,
                }

                if endpoint_name == "Wikidata":
                    params["format"] = "json"

                response = requests.get(
                    endpoint_url,
                    params=params,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/sparql-results+json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                    timeout=120,
                )

            except requests.RequestException as exc:
                print(
                    endpoint_name,
                    "connection error:",
                    exc,
                )
                time.sleep(5)
                continue

            if response.status_code == 200:
                try:
                    data = response.json()
                    rows = data.get(
                        "results",
                        {},
                    ).get(
                        "bindings",
                        [],
                    )

                    print(
                        endpoint_name,
                        "OK:",
                        len(rows),
                        "records",
                    )
                    return rows

                except Exception as exc:
                    print(
                        endpoint_name,
                        "invalid JSON:",
                        exc,
                    )
                    break

            if response.status_code == 429:
                print(
                    endpoint_name,
                    "RATE LIMITED (429)",
                )
                break

            if response.status_code in {502, 503, 504}:
                print(
                    endpoint_name,
                    "server overloaded:",
                    response.status_code,
                )
                break

            print(
                endpoint_name,
                "HTTP:",
                response.status_code,
            )
            print(response.text[:500])
            break

    raise RuntimeError(
        "Both QLever and Wikidata SPARQL are temporarily unavailable."
    )


def get_wikidata_entities(qids):
    qids = [qid for qid in qids if qid]

    if not qids:
        return {}

    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "labels|descriptions|claims",
        "languages": "en",
        "format": "json",
        "origin": "*",
    }

    last_error = None

    for attempt in range(1, 4):
        try:
            response = requests.get(
                WIKIDATA_API,
                params=params,
                headers={
                    "User-Agent": USER_AGENT,
                },
                timeout=60,
            )
            response.raise_for_status()

            return response.json().get(
                "entities",
                {},
            )

        except requests.RequestException as exc:
            last_error = exc
            print(
                f"Wikidata entity API error {attempt}/3:",
                exc,
            )
            time.sleep(attempt * 3)

    raise RuntimeError(
        f"Wikidata entity API failed after 3 attempts: {last_error}"
    )


def entity_label(entity):
    return clean(
        entity.get(
            "labels",
            {},
        ).get(
            "en",
            {},
        ).get(
            "value",
            "",
        )
    )


def entity_description(entity):
    return clean(
        entity.get(
            "descriptions",
            {},
        ).get(
            "en",
            {},
        ).get(
            "value",
            "",
        )
    )


def get_claims(entity, property_id):
    return entity.get(
        "claims",
        {},
    ).get(
        property_id,
        [],
    )


def claim_value(claim):
    try:
        return claim[
            "mainsnak"
        ][
            "datavalue"
        ][
            "value"
        ]
    except Exception:
        return None


def first_entity_claim(entity, property_id):
    for claim in get_claims(
        entity,
        property_id,
    ):
        data = claim_value(claim)

        if isinstance(data, dict) and data.get("id"):
            return data["id"]

    return ""


def all_entity_claims(entity, property_id, limit=5):
    values = []

    for claim in get_claims(
        entity,
        property_id,
    ):
        data = claim_value(claim)

        if isinstance(data, dict) and data.get("id"):
            values.append(data["id"])

        if len(values) >= limit:
            break

    return values


def first_string_claim(entity, property_id):
    for claim in get_claims(
        entity,
        property_id,
    ):
        data = claim_value(claim)

        if isinstance(data, str):
            return clean(data)

    return ""


def first_coordinate_claim(entity):
    for claim in get_claims(
        entity,
        "P625",
    ):
        data = claim_value(claim)

        if not isinstance(data, dict):
            continue

        lat = data.get("latitude")
        lon = data.get("longitude")

        if lat is not None and lon is not None:
            return float(lat), float(lon)

    return None


def first_time_claim(entity, property_id):
    """
    Returns:
        full_date: YYYY-MM-DD only when Wikidata has day precision.
        display: precision-preserving YYYY / YYYY-MM / YYYY-MM-DD.
    """
    for claim in get_claims(
        entity,
        property_id,
    ):
        data = claim_value(claim)

        if not isinstance(data, dict):
            continue

        raw_time = clean(data.get("time"))
        precision = int(data.get("precision", 0))

        match = re.search(
            r"([+-]?\d+)-(\d{2})-(\d{2})",
            raw_time,
        )

        if not match:
            continue

        year = match.group(1).lstrip("+")
        month = match.group(2)
        day = match.group(3)

        display = year

        if precision >= 10:
            display = f"{year}-{month}"

        if precision >= 11:
            display = f"{year}-{month}-{day}"
        full_date = display if precision >= 11 else ""
        return full_date, display

    return "", ""


def resolve_labels(qids):
    qids = list(
        dict.fromkeys(
            qid
            for qid in qids
            if qid
        )
    )

    if not qids:
        return {}

    result = {}

    for start in range(0, len(qids), 40):
        chunk = qids[start:start + 40]
        entities = get_wikidata_entities(chunk)

        for qid, entity in entities.items():
            result[qid] = entity_label(entity) or qid

    return result


def get_person_details(person_qid):
    entities = get_wikidata_entities(
        [person_qid]
    )

    person = entities.get(
        person_qid,
        {},
    )

    name = entity_label(person)
    description = entity_description(person)

    dob_full, dob_display = first_time_claim(
        person,
        "P569",
    )

    dod_full, dod_display = first_time_claim(
        person,
        "P570",
    )

    birth_place_qid = first_entity_claim(
        person,
        "P19",
    )

    death_place_qid = first_entity_claim(
        person,
        "P20",
    )

    occupation_qids = all_entity_claims(
        person,
        "P106",
        limit=4,
    )

    citizenship_qids = all_entity_claims(
        person,
        "P27",
        limit=3,
    )

    labels = resolve_labels(
        [
            birth_place_qid,
            death_place_qid,
        ]
        + occupation_qids
        + citizenship_qids
    )

    return {
        "name": name,
        "wikidata_description": description,
        "date_of_birth": dob_full,
        "date_of_birth_display": dob_display,
        "date_of_death": dod_full,
        "date_of_death_display": dod_display,
        "city_of_birth": labels.get(
            birth_place_qid,
            "",
        ),
        "city_of_death": labels.get(
            death_place_qid,
            "",
        ),
        "occupations": [
            labels[qid]
            for qid in occupation_qids
            if qid in labels
        ],
        "citizenships": [
            labels[qid]
            for qid in citizenship_qids
            if qid in labels
        ],
    }


def get_cemetery_wikidata_details(cemetery_qid):
    entities = get_wikidata_entities(
        [cemetery_qid]
    )

    cemetery = entities.get(
        cemetery_qid,
        {},
    )

    admin_qid = first_entity_claim(
        cemetery,
        "P131",
    )

    labels = resolve_labels(
        [admin_qid]
    )

    return {
        "name": entity_label(cemetery),
        "wikidata_description": entity_description(cemetery),
        "coord": first_coordinate_claim(cemetery),
        "admin": labels.get(admin_qid, ""),
        "website": first_string_claim(cemetery, "P856"),
    }


# ============================================================
# OPENSTREETMAP / NOMINATIM
# ============================================================


def osm_result_url(item):
    osm_type = clean(
        item.get("osm_type")
    ).lower()
    osm_id = clean(
        item.get("osm_id")
    )

    if (
        osm_type in {"node", "way", "relation"}
        and osm_id
    ):
        return (
            "https://www.openstreetmap.org/"
            + osm_type
            + "/"
            + osm_id
        )

    return ""


def select_best_osm_result(results, cemetery_name, admin_name=""):
    if not results:
        return {}

    source_norm = normalize_name(cemetery_name)
    admin_norm = normalize_name(admin_name)
    scored = []

    for item in results:
        address = item.get("address", {}) or {}
        country_code = clean(
            address.get("country_code")
        ).lower()

        if (
            country_code
            and country_code != NOMINATIM_COUNTRY_CODE
        ):
            continue

        candidate_name = first_non_empty(
            item.get("name"),
            clean(item.get("display_name")).split(",")[0],
        )
        candidate_norm = normalize_name(candidate_name)

        if not source_norm or not candidate_norm:
            continue

        name_score = fuzz.ratio(
            source_norm,
            candidate_norm,
        )

        display_norm = normalize_name(
            item.get("display_name", "")
        )

        admin_bonus = 0
        if admin_norm and admin_norm in display_norm:
            admin_bonus = 8

        item_type = clean(item.get("type")).lower()
        class_name = clean(item.get("class")).lower()
        cemetery_bonus = 0

        if item_type in {
            "cemetery",
            "grave_yard",
        } or class_name == "place":
            cemetery_bonus = 3

        scored.append(
            (
                name_score + admin_bonus + cemetery_bonus,
                name_score,
                item,
            )
        )

    if not scored:
        return {}

    scored.sort(
        key=lambda row: (
            -row[0],
            -row[1],
        )
    )

    _, best_name_score, best_item = scored[0]

    # Avoid using an unrelated first Nominatim result as location evidence.
    if best_name_score < 80:
        return {}

    return best_item


def nominatim_search(cemetery_name, admin_name=""):
    require_db()

    search_key = "|".join(
        [
            NOMINATIM_COUNTRY_CODE,
            normalize_name(cemetery_name),
            normalize_name(admin_name),
        ]
    )

    cached = db.execute(
        """
        SELECT result_json
        FROM osm_cache
        WHERE search_key = ?
        """,
        (search_key,),
    ).fetchone()

    if cached:
        try:
            return json.loads(cached[0])
        except Exception:
            pass

    query_parts = [cemetery_name]

    if admin_name:
        query_parts.append(admin_name)

    query_parts.append(COUNTRY_NAME)
    query = ", ".join(query_parts)

    print("OSM SEARCH:", query)

    # Public Nominatim usage should be deliberately slow.
    time.sleep(1.1)

    response = requests.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": NOMINATIM_COUNTRY_CODE,
        },
        headers={
            "User-Agent": USER_AGENT,
        },
        timeout=60,
    )

    response.raise_for_status()

    result = select_best_osm_result(
        response.json(),
        cemetery_name,
        admin_name,
    )

    db.execute(
        """
        INSERT OR REPLACE INTO osm_cache (
            search_key,
            result_json,
            searched_at
        )
        VALUES (?, ?, ?)
        """,
        (
            search_key,
            json.dumps(result),
            utc_now_iso(),
        ),
    )
    db.commit()

    return result


def extract_osm_fields(item):
    if not item:
        return {}

    address = item.get(
        "address",
        {},
    )

    city = first_non_empty(
        address.get("city"),
        address.get("town"),
        address.get("village"),
        address.get("municipality"),
        address.get("suburb"),
    )

    region = first_non_empty(
        address.get("county"),
        address.get("state"),
        address.get("region"),
    )

    street = first_non_empty(
        address.get("road"),
        address.get("pedestrian"),
        address.get("residential"),
    )

    return {
        "latitude": clean(item.get("lat")),
        "longitude": clean(item.get("lon")),
        "city": city,
        "region": region,
        "street": street,
        "postal_code": clean(
            address.get("postcode")
        ),
        "location": clean(
            item.get("display_name")
        ),
        "source_url": osm_result_url(item),
    }


# ============================================================
# OPENAI WEB RESEARCH
# ============================================================


def extract_urls_from_object(obj):
    urls = set()

    if isinstance(obj, dict):
        if obj.get("type") == "url_citation":
            url = clean(obj.get("url"))

            if url:
                urls.add(url)

        for item in obj.values():
            urls.update(
                extract_urls_from_object(item)
            )

    elif isinstance(obj, list):
        for item in obj:
            urls.update(
                extract_urls_from_object(item)
            )

    return urls


def clean_generated_description(text):
    text = clean(text)

    # Keep the published description as plain prose.
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def fallback_cemetery_description(cemetery_name, admin_name):
    if admin_name:
        return (
            f"{cemetery_name} is a cemetery located in "
            f"{admin_name}, {COUNTRY_NAME}."
        )

    return (
        f"{cemetery_name} is a cemetery located in "
        f"{COUNTRY_NAME}."
    )


def research_cemetery_description(
    cemetery_key,
    cemetery_name,
    admin_name,
    osm_location="",
    wikidata_description="",
):
    require_db()

    cached = db.execute(
        """
        SELECT description, sources_json
        FROM cemetery_research
        WHERE cemetery_key = ?
        """,
        (cemetery_key,),
    ).fetchone()

    if cached:
        try:
            sources = json.loads(
                cached[1] or "[]"
            )
        except Exception:
            sources = []

        return cached[0] or "", sources

    location_context = first_non_empty(
        osm_location,
        admin_name,
    )

    prompt = f"""
Research this cemetery in {COUNTRY_NAME} using web search.

Cemetery:
{cemetery_name}

Location context:
{location_context or COUNTRY_NAME}

Existing Wikidata description:
{wikidata_description or "None"}

Write a concise factual public description of 2 to 4 sentences.

Requirements:
- State where the cemetery is located when this can be verified.
- Add useful factual context such as denomination, opening period,
  authority, history, size or notable characteristics only when a
  reliable source supports it.
- Do not invent facts.
- Do not speculate.
- Do not list sources or URLs in the description.
- Do not mention Wikidata, OpenStreetMap, SEO or this research task.
- Do not use markdown.
- If very little reliable information exists, give only a simple
  factual location description.
"""

    description = ""
    sources = []

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            tools=[
                {
                    "type": "web_search",
                }
            ],
            input=prompt,
            max_output_tokens=300,
        )

        description = clean_generated_description(
            response.output_text
        )

        try:
            sources = sorted(
                extract_urls_from_object(
                    response.model_dump()
                )
            )
        except Exception:
            sources = []

    except Exception as exc:
        print(
            "OpenAI research failed:",
            exc,
        )

    # Generated historical/contextual prose is only retained when the
    # web-search response provides source URLs. Otherwise use a simple,
    # deterministic location statement rather than unsupported prose.
    if len(description) < 30 or not sources:
        description = fallback_cemetery_description(
            cemetery_name,
            admin_name,
        )
        sources = []

    sources = dedupe_urls(sources)

    db.execute(
        """
        INSERT OR REPLACE INTO cemetery_research (
            cemetery_key,
            description,
            sources_json,
            researched_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            cemetery_key,
            description,
            json.dumps(sources),
            utc_now_iso(),
        ),
    )
    db.commit()

    return description, sources


# ============================================================
# CEMETERY MATCHING
# ============================================================


def candidate_coordinates(candidate):
    try:
        return (
            float(candidate.get("latitude")),
            float(candidate.get("longitude")),
        )
    except Exception:
        return None


def find_existing_cemetery(
    source_name,
    source_admin,
    source_coord,
    local_cemeteries,
):
    source_norm = normalize_name(source_name)
    admin_norm = normalize_name(source_admin)

    if not source_norm:
        return None

    exact = [
        cemetery
        for cemetery in local_cemeteries
        if cemetery.get("_normalized") == source_norm
    ]

    # Unique exact-name matches still require corroborating location
    # evidence. A name alone is not enough to merge two places.
    if len(exact) == 1:
        candidate = exact[0]
        candidate_coord = candidate_coordinates(candidate)
        context = cemetery_context(candidate)

        if source_coord and candidate_coord:
            km = distance_km(
                source_coord[0],
                source_coord[1],
                candidate_coord[0],
                candidate_coord[1],
            )
            return candidate if km <= 30 else None

        if admin_norm and admin_norm in context:
            return candidate

        return None

    # Same name several times: administrative context first.
    if len(exact) > 1 and admin_norm:
        admin_matches = [
            candidate
            for candidate in exact
            if admin_norm in cemetery_context(candidate)
        ]

        if len(admin_matches) == 1:
            return admin_matches[0]

    # Same name several times: geographic confirmation.
    if len(exact) > 1 and source_coord:
        candidates = []

        for candidate in exact:
            coord = candidate_coordinates(candidate)

            if not coord:
                continue

            km = distance_km(
                source_coord[0],
                source_coord[1],
                coord[0],
                coord[1],
            )
            candidates.append((km, candidate))

        if candidates:
            candidates.sort(key=lambda item: item[0])

            if candidates[0][0] <= 20:
                return candidates[0][1]

    # Fuzzy matching is only allowed with geographic confirmation.
    if source_coord:
        matches = []

        for candidate in local_cemeteries:
            candidate_norm = candidate.get(
                "_normalized",
                "",
            )

            if not candidate_norm:
                continue

            score = fuzz.ratio(
                source_norm,
                candidate_norm,
            )

            if score < 94:
                continue

            coord = candidate_coordinates(candidate)

            if not coord:
                continue

            km = distance_km(
                source_coord[0],
                source_coord[1],
                coord[0],
                coord[1],
            )

            if km <= 10:
                matches.append(
                    (
                        score,
                        km,
                        candidate,
                    )
                )

        if matches:
            matches.sort(
                key=lambda item: (
                    -item[0],
                    item[1],
                )
            )
            return matches[0][2]

    return None


# ============================================================
# CEMETERY CREATE / ENRICH
# ============================================================


def get_full_cemetery(cemetery_id):
    return wp_get(
        f"/wp-json/cemetery-agent/v1/cemetery/{cemetery_id}"
    )


def enrich_existing_cemetery(
    cemetery,
    cemetery_qid,
    cemetery_name,
    admin_name,
    source_coord,
    wd_details,
):
    cemetery_id = int(cemetery["id"])

    print("READ CEMETERY:", cemetery_id)

    full = get_full_cemetery(cemetery_id)

    wikidata_url = (
        "https://www.wikidata.org/wiki/"
        + cemetery_qid
    )
    sources = [wikidata_url]

    current_lat = clean(full.get("latitude"))
    current_lon = clean(full.get("longitude"))
    current_city = clean(full.get("city"))

    needs_location = (
        not current_lat
        or not current_lon
        or not current_city
    )

    osm_fields = {}

    if needs_location:
        osm = nominatim_search(
            cemetery_name,
            first_non_empty(
                admin_name,
                wd_details.get("admin"),
            ),
        )
        osm_fields = extract_osm_fields(osm)

        if osm_fields.get("source_url"):
            sources.append(
                osm_fields["source_url"]
            )

    wd_coord = (
        wd_details.get("coord")
        or source_coord
    )

    latitude = first_non_empty(
        osm_fields.get("latitude"),
        str(wd_coord[0]) if wd_coord else "",
    )

    longitude = first_non_empty(
        osm_fields.get("longitude"),
        str(wd_coord[1]) if wd_coord else "",
    )

    current_description = clean(
        full.get("description")
    )
    plain_current_description = re.sub(
        r"<[^>]+>",
        "",
        current_description,
    ).strip()

    description = ""

    if len(plain_current_description) < 30:
        description, research_sources = (
            research_cemetery_description(
                cemetery_qid,
                cemetery_name,
                first_non_empty(
                    osm_fields.get("region"),
                    admin_name,
                    wd_details.get("admin"),
                ),
                osm_fields.get("location", ""),
                wd_details.get(
                    "wikidata_description",
                    "",
                ),
            )
        )
        sources.extend(research_sources)

    payload = compact_payload(
        {
            "description": description,
            "city": first_non_empty(
                osm_fields.get("city"),
                admin_name,
                wd_details.get("admin"),
            ),
            "region": first_non_empty(
                osm_fields.get("region"),
                admin_name,
            ),
            "country": WP_COUNTRY,
            "street": osm_fields.get(
                "street",
                "",
            ),
            "postal_code": osm_fields.get(
                "postal_code",
                "",
            ),
            "latitude": latitude,
            "longitude": longitude,
            "location": osm_fields.get(
                "location",
                "",
            ),
            "website": wd_details.get(
                "website",
                "",
            ),
            "external_cemetery_id": cemetery_qid,
            "source_urls": dedupe_urls(sources),
        }
    )

    result = wp_post(
        f"/wp-json/cemetery-agent/v1/cemetery/{cemetery_id}/merge",
        payload,
    )

    print(
        "CEMETERY UPDATED:",
        result.get("updated_fields"),
    )

    return cemetery_id


def has_sufficient_cemetery_location(
    latitude,
    longitude,
    city,
    region,
):
    has_coordinates = bool(
        clean(latitude)
        and clean(longitude)
    )
    has_admin_context = bool(
        clean(city)
        or clean(region)
    )

    return has_coordinates or has_admin_context


def create_new_cemetery(
    cemetery_qid,
    cemetery_name,
    admin_name,
    source_coord,
    wd_details,
):
    print("NEW CEMETERY:", cemetery_name)

    osm = nominatim_search(
        cemetery_name,
        first_non_empty(
            admin_name,
            wd_details.get("admin"),
        ),
    )
    osm_fields = extract_osm_fields(osm)

    wd_coord = (
        wd_details.get("coord")
        or source_coord
    )

    latitude = first_non_empty(
        osm_fields.get("latitude"),
        str(wd_coord[0]) if wd_coord else "",
    )

    longitude = first_non_empty(
        osm_fields.get("longitude"),
        str(wd_coord[1]) if wd_coord else "",
    )

    city = first_non_empty(
        osm_fields.get("city"),
        admin_name,
        wd_details.get("admin"),
    )

    region = first_non_empty(
        osm_fields.get("region"),
        admin_name,
    )

    if not has_sufficient_cemetery_location(
        latitude,
        longitude,
        city,
        region,
    ):
        raise InsufficientEvidenceError(
            "cemetery not found and structured location evidence is insufficient"
        )

    wikidata_url = (
        "https://www.wikidata.org/wiki/"
        + cemetery_qid
    )

    sources = [wikidata_url]

    if osm_fields.get("source_url"):
        sources.append(
            osm_fields["source_url"]
        )

    print("RESEARCHING CEMETERY:", cemetery_name)

    description, research_sources = (
        research_cemetery_description(
            cemetery_qid,
            cemetery_name,
            region or city,
            osm_fields.get("location", ""),
            wd_details.get(
                "wikidata_description",
                "",
            ),
        )
    )
    sources.extend(research_sources)
    sources = dedupe_urls(sources)

    # Creation itself cites only the structured Wikidata record, whose data
    # is CC0. OSM/web sources are attached afterwards without incorrectly
    # labelling the whole mixed source set as CC0.
    create_payload = {
        "name": cemetery_name,
        "country": WP_COUNTRY,
        "city": city,
        "region": region,
        "latitude": latitude,
        "longitude": longitude,
        "external_id": cemetery_qid,
        "source_urls": [wikidata_url],
        "source_license": "CC0",
        "source_reuse_allowed": True,
    }

    result = wp_post(
        "/wp-json/cemetery-agent/v1/cemetery",
        create_payload,
    )

    cemetery_id = int(
        result["cemetery_id"]
    )

    print("CEMETERY CREATED:", cemetery_id)

    merge_payload = compact_payload(
        {
            "description": description,
            "city": city,
            "region": region,
            "country": WP_COUNTRY,
            "street": osm_fields.get(
                "street",
                "",
            ),
            "postal_code": osm_fields.get(
                "postal_code",
                "",
            ),
            "latitude": latitude,
            "longitude": longitude,
            "location": osm_fields.get(
                "location",
                "",            ),
            "website": wd_details.get(
                "website",
                "",
            ),
            "external_cemetery_id": cemetery_qid,
            "source_urls": sources,
        }
    )

    merge_result = wp_post(
        f"/wp-json/cemetery-agent/v1/cemetery/{cemetery_id}/merge",
        merge_payload,
    )

    print(
        "CEMETERY ENRICHED:",
        merge_result.get("updated_fields"),
    )

    return cemetery_id, {
        "id": cemetery_id,
        "title": result.get(
            "name",
            cemetery_name,
        ),
        "city": city,
        "region": region,
        "country": WP_COUNTRY,
        "latitude": latitude,
        "longitude": longitude,
        "_normalized": normalize_name(
            cemetery_name
        ),
    }


# ============================================================
# GRAVE HELPERS
# ============================================================


def get_existing_graves(cemetery_id):
    return wp_get(
        "/wp-json/cemetery-agent/v1/graves",
        {
            "cemetery_id": cemetery_id,
        },
    )


def find_existing_grave(
    graves,
    external_id,
    person_name,
):
    normalized_name = clean(
        person_name
    ).lower()

    # Strong match: exact external identifier.
    for grave in graves:
        existing_external = clean(
            grave.get("external_grave_id")
        )

        if (
            existing_external
            and existing_external == external_id
        ):
            return grave

    # Legacy fallback: an exact name may be used only when it identifies one
    # record and that record does not already carry a conflicting external ID.
    name_matches = []

    for grave in graves:
        existing_name = clean(
            grave.get("title")
        ).lower()
        existing_external = clean(
            grave.get("external_grave_id")
        )

        if (
            existing_name == normalized_name
            and not existing_external
        ):
            name_matches.append(grave)

    if len(name_matches) == 1:
        return name_matches[0]

    return None


# ============================================================
# PERSON / GRAVE ENRICHMENT
# ============================================================


def build_person_description(
    person_name,
    details,
    cemetery_name,
    admin_name,
):
    sentences = []

    wd_description = clean(
        details.get("wikidata_description")
    )

    if wd_description:
        wd_description = (
            wd_description[0].upper()
            + wd_description[1:]
        )

        if not wd_description.endswith("."):
            wd_description += "."

        sentences.append(wd_description)

    dob = clean(
        details.get("date_of_birth_display")
    )
    dod = clean(
        details.get("date_of_death_display")
    )

    date_parts = []

    if dob:
        date_parts.append("born " + dob)

    if dod:
        date_parts.append("died " + dod)

    if date_parts:
        sentences.append(
            person_name
            + " was "
            + " and ".join(date_parts)
            + "."
        )

    birth_place = clean(
        details.get("city_of_birth")
    )
    death_place = clean(
        details.get("city_of_death")
    )

    if birth_place:
        sentences.append(
            "Place of birth: "
            + birth_place
            + "."
        )

    if death_place:
        sentences.append(
            "Place of death: "
            + death_place
            + "."
        )

    occupations = details.get(
        "occupations",
        [],
    )

    if occupations:
        sentences.append(
            "Occupation: "
            + ", ".join(occupations[:3])
            + "."
        )

    citizenships = details.get(
        "citizenships",
        [],
    )

    if citizenships:
        sentences.append(
            "Citizenship: "
            + ", ".join(citizenships[:2])
            + "."
        )

    burial_text = (
        "The recorded burial place is "
        + cemetery_name
    )

    if admin_name:
        burial_text += " in " + admin_name

    burial_text += "."
    sentences.append(burial_text)

    return " ".join(sentences)


def process_grave(
    person_qid,
    cemetery_qid,
    person_name,
    cemetery_name,
    admin_name,
    cemetery_id,
):
    external_id = (
        person_qid
        + "@"
        + cemetery_qid
    )

    details = get_person_details(person_qid)

    actual_name = first_non_empty(
        details.get("name"),
        person_name,
    )

    first_name, surname, second_surname = split_name(
        actual_name
    )

    description = build_person_description(
        actual_name,
        details,
        cemetery_name,
        admin_name,
    )

    graves = get_existing_graves(cemetery_id)
    existing = find_existing_grave(
        graves,
        external_id,
        actual_name,
    )

    wikidata_url = (
        "https://www.wikidata.org/wiki/"
        + person_qid
    )
    source_urls = [wikidata_url]

    common_payload = compact_payload(
        {
            "description": description,
            "first_name": first_name,
            "first_surname": surname,
            "second_surname": second_surname,
            "date_of_birth": details.get(
                "date_of_birth",
                "",
            ),
            "date_of_death": details.get(
                "date_of_death",
                "",
            ),
            "city_of_birth": details.get(
                "city_of_birth",
                "",
            ),
            "city_of_death": details.get(
                "city_of_death",
                "",
            ),
            "external_grave_id": external_id,
            "source_urls": source_urls,
        }
    )

    if existing:
        grave_post_id = int(existing["id"])

        print("GRAVE EXISTS:", grave_post_id)

        result = wp_post(
            f"/wp-json/cemetery-agent/v1/grave/{grave_post_id}/merge",
            common_payload,
        )

        updated_fields = result.get(
            "updated_fields"
        )

        print(
            "GRAVE UPDATED:",
            updated_fields,
        )

        if isinstance(updated_fields, list) and not updated_fields:
            return "UNCHANGED", grave_post_id

        return "UPDATED", grave_post_id

    create_payload = {
        "cemetery_id": cemetery_id,
        "full_name": actual_name,
        "first_name": first_name,
        "first_surname": surname,
        "second_surname": second_surname,
        "date_of_birth": details.get(
            "date_of_birth",
            "",
        ),
        "date_of_death": details.get(
            "date_of_death",
            "",
        ),
        "city_of_birth": details.get(
            "city_of_birth",
            "",
        ),
        "city_of_death": details.get(
            "city_of_death",
            "",
        ),
        "plot_location": "",
        "epitaph": "",
        "grave_id": external_id,
        "description": description,
        "source_urls": source_urls,
        "source_license": "CC0",
        "source_reuse_allowed": True,
    }

    result = wp_post(
        "/wp-json/cemetery-agent/v1/grave",
        create_payload,
    )

    if result.get("status") == "duplicate":
        grave_post_id = int(
            result.get("existing_id")
        )

        print(
            "GRAVE DUPLICATE:",
            grave_post_id,
        )
        return "DUPLICATE", grave_post_id

    grave_post_id = int(
        result["grave_post_id"]
    )

    print("GRAVE CREATED:", grave_post_id)
    print(result.get("grave_url"))

    return "CREATED", grave_post_id


# ============================================================
# MAIN WORKFLOW
# ============================================================


def fetch_discovery_batch(offset):
    while True:
        try:
            return wikidata_query(
                BATCH_SIZE,
                offset,
            )

        except RuntimeError as exc:
            print(
                "SPARQL temporarily unavailable:",
                exc,
            )
            print(
                f"Retrying in {SPARQL_RETRY_SECONDS} seconds..."
            )
            time.sleep(SPARQL_RETRY_SECONDS)


def print_summary(
    title,
    handled,
    cemeteries_created,
    graves_created,
    graves_updated,
    graves_unchanged,
    duplicates,
    skipped,
    errors,
):
    print()
    print("======================================")
    print(title)
    print("People handled:", handled)
    print("Cemeteries created:", cemeteries_created)
    print("Graves created:", graves_created)
    print("Graves updated:", graves_updated)
    print("Graves unchanged:", graves_unchanged)
    print("Duplicates:", duplicates)
    print("Skipped / abstained:", skipped)
    print("Errors:", errors)
    print("======================================")


def main():
    global db

    init_database()

    try:
        print()
        print("======================================")
        print(
            f"CEMETERYLINK {COUNTRY_NAME.upper()} ENRICHMENT AGENT"
        )
        print("======================================")
        print("Country QID:", COUNTRY_QID)
        print("State DB:", DB_FILE.name)
        print("Batch size:", BATCH_SIZE)
        print(
            "Run limit:",
            MAX_PEOPLE_PER_RUN
            if MAX_PEOPLE_PER_RUN
            else "unlimited",
        )

        local_cemeteries = load_wp_cemeteries()
        offset, finished = get_cursor()

        if finished:
            print(
                f"{COUNTRY_NAME} was already marked as finished."
            )
            return

        handled_this_run = 0
        total_graves_created = 0
        total_graves_updated = 0
        total_graves_unchanged = 0
        total_duplicates = 0
        total_cemeteries_created = 0
        total_skipped = 0
        total_errors = 0

        while True:
            print()
            print("Wikidata offset:", offset)

            rows = fetch_discovery_batch(offset)
            print("Records:", len(rows))

            if not rows:
                save_cursor(
                    offset,
                    finished=True,
                )
                break

            for row in rows:
                person_qid = qid_from_uri(
                    value(row, "person")
                )
                cemetery_qid = qid_from_uri(
                    value(row, "cemetery")
                )
                person_name = value(
                    row,
                    "personLabel",
                )
                cemetery_name = value(
                    row,
                    "cemeteryLabel",
                )
                admin_name = value(
                    row,
                    "adminLabel",
                )
                source_coord = parse_point(
                    value(row, "coord")
                )

                if (
                    not person_qid
                    or not cemetery_qid
                    or not person_name
                    or not cemetery_name
                ):
                    continue

                external_id = (
                    person_qid
                    + "@"
                    + cemetery_qid
                )

                if already_processed(external_id):
                    continue

                print()
                print("--------------------------------------")
                print("PERSON:", person_name)
                print("CEMETERY:", cemetery_name)
                print("ADMIN:", admin_name or "(none)")
                print("--------------------------------------")

                try:
                    wd_cemetery = (
                        get_cemetery_wikidata_details(
                            cemetery_qid
                        )
                    )

                    if not admin_name:
                        admin_name = clean(
                            wd_cemetery.get("admin")
                        )

                    source_coord = (
                        source_coord
                        or wd_cemetery.get("coord")
                    )

                    cemetery = find_existing_cemetery(
                        cemetery_name,
                        admin_name,
                        source_coord,
                        local_cemeteries,
                    )

                    if cemetery:
                        print(
                            "CEMETERY EXISTS:",
                            cemetery["id"],
                        )

                        cemetery_id = (
                            enrich_existing_cemetery(
                                cemetery,
                                cemetery_qid,
                                cemetery_name,
                                admin_name,
                                source_coord,
                                wd_cemetery,
                            )
                        )

                    else:
                        cemetery_id, new_cemetery = (
                            create_new_cemetery(
                                cemetery_qid,
                                cemetery_name,
                                admin_name,
                                source_coord,
                                wd_cemetery,
                            )
                        )

                        local_cemeteries.append(
                            new_cemetery
                        )
                        total_cemeteries_created += 1

                    status, grave_post_id = process_grave(
                        person_qid,
                        cemetery_qid,
                        person_name,
                        cemetery_name,
                        admin_name,
                        cemetery_id,
                    )

                    if status == "CREATED":
                        total_graves_created += 1
                    elif status == "UPDATED":
                        total_graves_updated += 1
                    elif status == "UNCHANGED":
                        total_graves_unchanged += 1
                    elif status == "DUPLICATE":
                        total_duplicates += 1

                    mark_processed(
                        external_id,
                        status,
                        cemetery_id=cemetery_id,
                        grave_id=grave_post_id,
                    )

                except InsufficientEvidenceError as exc:
                    print(
                        "SKIPPED:",
                        exc,
                    )
                    mark_processed(
                        external_id,
                        "SKIPPED",
                        reason=str(exc),
                    )
                    total_skipped += 1

                except Exception as exc:
                    print(
                        "ERROR:",
                        repr(exc),
                    )
                    mark_processed(
                        external_id,
                        "ERROR",
                        reason=str(exc),
                    )
                    total_errors += 1

                handled_this_run += 1

                if (
                    MAX_PEOPLE_PER_RUN
                    and handled_this_run >= MAX_PEOPLE_PER_RUN
                ):
                    print_summary(
                        "RUN LIMIT REACHED",
                        handled_this_run,
                        total_cemeteries_created,
                        total_graves_created,
                        total_graves_updated,
                        total_graves_unchanged,
                        total_duplicates,
                        total_skipped,
                        total_errors,
                    )
                    return

            offset += len(rows)

            save_cursor(
                offset,
                finished=False,
            )

            if len(rows) < BATCH_SIZE:
                save_cursor(
                    offset,
                    finished=True,
                )
                break

            time.sleep(SLEEP_BETWEEN_BATCHES)

        print_summary(
            f"{COUNTRY_NAME.upper()} COMPLETE",
            handled_this_run,
            total_cemeteries_created,
            total_graves_created,
            total_graves_updated,
            total_graves_unchanged,
            total_duplicates,
            total_skipped,
            total_errors,
        )

    finally:
        if db is not None:
            db.close()
            db = None


if __name__ == "__main__":
    main()
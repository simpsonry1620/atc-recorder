"""OpenSky Network API client for ADS-B flight data enrichment."""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .logging import get_logger

logger = get_logger(__name__)

OPENSKY_API_BASE = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
DEFAULT_CACHE_TTL = 3600  # 1 hour
DEFAULT_TIMEOUT = 15.0


@dataclass
class FlightEnrichment:
    """Enrichment data for a flight from OpenSky Network."""

    callsign: str
    icao24: str = ""
    registration: str = ""
    aircraft_type: str = ""
    origin: str = ""
    destination: str = ""
    first_seen_utc: Optional[datetime] = None
    last_seen_utc: Optional[datetime] = None
    last_latitude: Optional[float] = None
    last_longitude: Optional[float] = None
    last_altitude: Optional[float] = None
    feeds_heard: list[str] = field(default_factory=list)
    enriched_at: Optional[datetime] = None
    source: str = "opensky"


class FlightEnrichmentStore:
    """SQLite cache for OpenSky flight enrichment data."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS flight_enrichment (
                    callsign TEXT PRIMARY KEY,
                    icao24 TEXT,
                    registration TEXT,
                    aircraft_type TEXT,
                    origin TEXT,
                    destination TEXT,
                    first_seen_utc TEXT,
                    last_seen_utc TEXT,
                    last_latitude REAL,
                    last_longitude REAL,
                    last_altitude REAL,
                    feeds_heard TEXT,
                    enriched_at TEXT,
                    source TEXT DEFAULT 'opensky'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enrich_icao24 ON flight_enrichment(icao24)"
            )
            conn.commit()

    def get(self, callsign: str) -> Optional[FlightEnrichment]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM flight_enrichment WHERE callsign = ?", (callsign,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_enrichment(dict(row))

    def get_all(self, limit: int = 200) -> list[FlightEnrichment]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM flight_enrichment ORDER BY enriched_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_enrichment(dict(r)) for r in rows]

    def upsert(self, enrichment: FlightEnrichment) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO flight_enrichment (
                    callsign, icao24, registration, aircraft_type,
                    origin, destination, first_seen_utc, last_seen_utc,
                    last_latitude, last_longitude, last_altitude,
                    feeds_heard, enriched_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(callsign) DO UPDATE SET
                    icao24=excluded.icao24,
                    registration=excluded.registration,
                    aircraft_type=excluded.aircraft_type,
                    origin=excluded.origin,
                    destination=excluded.destination,
                    first_seen_utc=excluded.first_seen_utc,
                    last_seen_utc=excluded.last_seen_utc,
                    last_latitude=excluded.last_latitude,
                    last_longitude=excluded.last_longitude,
                    last_altitude=excluded.last_altitude,
                    feeds_heard=excluded.feeds_heard,
                    enriched_at=excluded.enriched_at,
                    source=excluded.source
                """,
                (
                    enrichment.callsign,
                    enrichment.icao24,
                    enrichment.registration,
                    enrichment.aircraft_type,
                    enrichment.origin,
                    enrichment.destination,
                    enrichment.first_seen_utc.isoformat() if enrichment.first_seen_utc else None,
                    enrichment.last_seen_utc.isoformat() if enrichment.last_seen_utc else None,
                    enrichment.last_latitude,
                    enrichment.last_longitude,
                    enrichment.last_altitude,
                    json.dumps(enrichment.feeds_heard),
                    enrichment.enriched_at.isoformat() if enrichment.enriched_at else None,
                    enrichment.source,
                ),
            )
            conn.commit()

    @staticmethod
    def _row_to_enrichment(row: dict) -> FlightEnrichment:
        feeds_raw = row.get("feeds_heard", "[]")
        try:
            feeds = json.loads(feeds_raw) if feeds_raw else []
        except (json.JSONDecodeError, TypeError):
            feeds = []

        def _parse_dt(val: Optional[str]) -> Optional[datetime]:
            if not val:
                return None
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (ValueError, AttributeError):
                return None

        return FlightEnrichment(
            callsign=row["callsign"],
            icao24=row.get("icao24") or "",
            registration=row.get("registration") or "",
            aircraft_type=row.get("aircraft_type") or "",
            origin=row.get("origin") or "",
            destination=row.get("destination") or "",
            first_seen_utc=_parse_dt(row.get("first_seen_utc")),
            last_seen_utc=_parse_dt(row.get("last_seen_utc")),
            last_latitude=row.get("last_latitude"),
            last_longitude=row.get("last_longitude"),
            last_altitude=row.get("last_altitude"),
            feeds_heard=feeds,
            enriched_at=_parse_dt(row.get("enriched_at")),
            source=row.get("source", "opensky"),
        )


class OpenSkyClient:
    """Client for the OpenSky Network REST API.

    Uses OAuth2 client_credentials for authentication (credentials.json).
    The /states/all endpoint does NOT support a callsign filter parameter.
    Instead we fetch states within a geographic bounding box around the airport
    and build a callsign -> ICAO24 lookup locally.
    """

    DEFAULT_BBOX = {"lamin": 38.0, "lamax": 39.7, "lomin": -77.9, "lomax": -76.2}

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        cache_ttl: int = DEFAULT_CACHE_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        bbox: Optional[dict] = None,
    ):
        self.session = requests.Session()
        self._client_id = client_id
        self._client_secret = client_secret
        self.has_auth = bool(client_id and client_secret)
        self._access_token: Optional[str] = None
        self._token_expires_at = 0.0
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.bbox = bbox or self.DEFAULT_BBOX
        self._request_count = 0
        self._last_request_time = 0.0
        self._callsign_cache: dict[str, dict] = {}
        self._callsign_cache_time = 0.0
        self._flights_forbidden = False

    @classmethod
    def from_credentials_file(
        cls,
        credentials_path: Path,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        bbox: Optional[dict] = None,
    ) -> "OpenSkyClient":
        """Create client from a credentials.json file with clientId/clientSecret."""
        with open(credentials_path) as f:
            creds = json.load(f)
        return cls(
            client_id=creds.get("clientId", ""),
            client_secret=creds.get("clientSecret", ""),
            cache_ttl=cache_ttl,
            timeout=timeout,
            bbox=bbox,
        )

    def _acquire_token(self) -> Optional[str]:
        """Get an OAuth2 access token using client_credentials grant."""
        if not self.has_auth:
            return None
        if self._access_token and time.time() < self._token_expires_at - 30:
            return self._access_token
        try:
            resp = requests.post(
                OPENSKY_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            token_data = resp.json()
            self._access_token = token_data["access_token"]
            self._token_expires_at = time.time() + token_data.get("expires_in", 300)
            logger.info("OpenSky OAuth2 token acquired (expires in %ds)", token_data.get("expires_in", 0))
            return self._access_token
        except requests.RequestException as exc:
            logger.warning("OpenSky token acquisition failed: %s", exc)
            self._access_token = None
            return None

    def _rate_limit_wait(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict | list]:
        self._rate_limit_wait()
        url = f"{OPENSKY_API_BASE}{path}"
        headers = {}
        token = self._acquire_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            self._last_request_time = time.time()
            self._request_count += 1
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code == 429:
                logger.warning("OpenSky rate limited (429). Backing off.")
                return None
            if resp.status_code == 403:
                logger.warning("OpenSky 403 Forbidden on %s (authentication required)", path)
                return None
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("OpenSky API request failed: %s", exc)
            return None

    def _refresh_callsign_cache(self) -> None:
        """Fetch all aircraft states in the bounding box and index by callsign."""
        age = time.time() - self._callsign_cache_time
        if age < 60 and self._callsign_cache:
            return

        data = self._get("/states/all", params=self.bbox)
        if not data or not isinstance(data, dict) or not data.get("states"):
            return

        self._callsign_cache = {}
        for state in data["states"]:
            cs = (state[1] or "").strip().upper()
            if cs:
                self._callsign_cache[cs] = {
                    "icao24": state[0],
                    "callsign": cs,
                    "origin_country": state[2],
                    "longitude": state[5],
                    "latitude": state[6],
                    "baro_altitude": state[7],
                    "on_ground": state[8],
                    "velocity": state[9],
                    "true_track": state[10],
                    "vertical_rate": state[11],
                    "geo_altitude": state[13],
                }
        self._callsign_cache_time = time.time()
        logger.info(
            "OpenSky bounding-box refresh: %d aircraft in view",
            len(self._callsign_cache),
        )

    def get_state_by_callsign(self, callsign: str) -> Optional[dict]:
        """Look up a callsign from the bounding-box state cache."""
        self._refresh_callsign_cache()
        return self._callsign_cache.get(callsign.upper())

    def get_flights_by_aircraft(
        self, icao24: str, begin: int, end: int
    ) -> list[dict]:
        """Get flight records for a specific aircraft in a time range.

        Requires OpenSky authentication. Returns empty list on 403.
        """
        if self._flights_forbidden:
            return []
        data = self._get(
            "/flights/aircraft",
            params={"icao24": icao24, "begin": begin, "end": end},
        )
        if data is None:
            self._flights_forbidden = True
            return []
        if not isinstance(data, list):
            return []
        return data

    def get_airport_arrivals(self, airport: str, begin: int, end: int) -> list[dict]:
        """Get flights that arrived at an airport in a time range (max 7 days)."""
        data = self._get(
            "/flights/arrival",
            params={"airport": airport, "begin": begin, "end": end},
        )
        if not data or not isinstance(data, list):
            return []
        return data

    def get_airport_departures(self, airport: str, begin: int, end: int) -> list[dict]:
        """Get flights that departed from an airport in a time range (max 7 days)."""
        data = self._get(
            "/flights/departure",
            params={"airport": airport, "begin": begin, "end": end},
        )
        if not data or not isinstance(data, list):
            return []
        return data

    def get_airport_flights(
        self, airport: str, begin: int, end: int, chunk_seconds: int = 86400
    ) -> dict[str, dict]:
        """Get all arrivals and departures for an airport, chunked into safe windows.

        The OpenSky arrival/departure endpoints accept a max window of ~1 day
        (86400 seconds) despite the docs claiming 7 days. Returns a dict keyed
        by callsign with the best flight record for each.
        """
        results: dict[str, dict] = {}
        window = chunk_seconds
        total_days = max(1, (end - begin) // 86400)
        t = begin
        day = 0
        while t < end:
            chunk_end = min(t + window, end)
            day += 1
            logger.info("Fetching %s day %d/%d ...", airport, day, total_days)
            arrivals = self.get_airport_arrivals(airport, t, chunk_end)
            departures = self.get_airport_departures(airport, t, chunk_end)
            for flight in arrivals:
                cs = (flight.get("callsign") or "").strip().upper()
                if cs:
                    results[cs] = flight
            for flight in departures:
                cs = (flight.get("callsign") or "").strip().upper()
                if cs and cs not in results:
                    results[cs] = flight
            logger.info(
                "  day %d: %d arrivals, %d departures, %d unique callsigns so far",
                day, len(arrivals), len(departures), len(results),
            )
            t = chunk_end
        return results

    def enrich_callsign(
        self, callsign: str, feeds_heard: Optional[list[str]] = None
    ) -> Optional[FlightEnrichment]:
        """Look up a callsign and return enrichment data."""
        state = self.get_state_by_callsign(callsign)
        enrichment = FlightEnrichment(
            callsign=callsign,
            feeds_heard=feeds_heard or [],
            enriched_at=datetime.now(timezone.utc),
        )

        if state:
            enrichment.icao24 = state.get("icao24", "")
            enrichment.last_latitude = state.get("latitude")
            enrichment.last_longitude = state.get("longitude")
            enrichment.last_altitude = state.get("baro_altitude")

            if enrichment.icao24 and self.has_auth:
                now = int(time.time())
                flights = self.get_flights_by_aircraft(
                    enrichment.icao24, begin=now - 86400, end=now
                )
                for flight in flights:
                    cs = (flight.get("callsign") or "").strip()
                    if cs.upper() == callsign.upper():
                        enrichment.origin = flight.get("estDepartureAirport") or ""
                        enrichment.destination = flight.get("estArrivalAirport") or ""
                        if flight.get("firstSeen"):
                            enrichment.first_seen_utc = datetime.fromtimestamp(
                                flight["firstSeen"], tz=timezone.utc
                            )
                        if flight.get("lastSeen"):
                            enrichment.last_seen_utc = datetime.fromtimestamp(
                                flight["lastSeen"], tz=timezone.utc
                            )
                        break

        return enrichment


class OpenSkyEnrichmentService:
    """Combines OpenSky API with local cache for enriching callsigns."""

    def __init__(
        self,
        db_path: Path,
        credentials_file: Optional[Path] = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        bbox: Optional[dict] = None,
    ):
        self.store = FlightEnrichmentStore(db_path)
        self.store.ensure_schema()
        if credentials_file and credentials_file.exists():
            self.client = OpenSkyClient.from_credentials_file(
                credentials_file, cache_ttl=cache_ttl, bbox=bbox,
            )
        else:
            self.client = OpenSkyClient(cache_ttl=cache_ttl, bbox=bbox)
        self.cache_ttl = cache_ttl

    def _is_cache_fresh(self, enrichment: FlightEnrichment) -> bool:
        if not enrichment.enriched_at:
            return False
        age = (datetime.now(timezone.utc) - enrichment.enriched_at).total_seconds()
        return age < self.cache_ttl

    def enrich(self, callsign: str, feeds_heard: Optional[list[str]] = None) -> Optional[FlightEnrichment]:
        """Enrich a callsign, using cache when fresh."""
        cached = self.store.get(callsign)
        if cached and self._is_cache_fresh(cached):
            if feeds_heard:
                merged = list(set(cached.feeds_heard + feeds_heard))
                if merged != cached.feeds_heard:
                    cached.feeds_heard = merged
                    self.store.upsert(cached)
            return cached

        result = self.client.enrich_callsign(callsign, feeds_heard=feeds_heard)
        if result:
            if cached:
                result.feeds_heard = list(set((cached.feeds_heard or []) + (feeds_heard or [])))
            self.store.upsert(result)
        return result

    def batch_enrich(self, callsigns: list[str], feeds_map: Optional[dict[str, list[str]]] = None) -> dict:
        """Enrich multiple callsigns. Returns stats."""
        stats = {"total": len(callsigns), "enriched": 0, "cached": 0, "failed": 0}
        for cs in callsigns:
            feeds = (feeds_map or {}).get(cs, [])
            cached = self.store.get(cs)
            if cached and self._is_cache_fresh(cached):
                stats["cached"] += 1
                continue
            result = self.enrich(cs, feeds_heard=feeds)
            if result and result.icao24:
                stats["enriched"] += 1
            else:
                stats["failed"] += 1
        return stats

    def historical_enrich(
        self,
        callsigns: list[str],
        feeds_map: Optional[dict[str, list[str]]],
        airport_flights: dict[str, dict],
        on_progress: Optional[callable] = None,
    ) -> dict:
        """Enrich callsigns using pre-fetched airport arrival/departure data.

        airport_flights should be the dict returned by
        OpenSkyClient.get_airport_flights().
        """
        stats = {"total": len(callsigns), "matched": 0, "not_found": 0, "api_flights": len(airport_flights)}

        for cs in callsigns:
            cs_upper = cs.upper()
            flight = airport_flights.get(cs_upper)
            if flight:
                enrichment = FlightEnrichment(
                    callsign=cs,
                    icao24=flight.get("icao24", ""),
                    origin=flight.get("estDepartureAirport") or "",
                    destination=flight.get("estArrivalAirport") or "",
                    feeds_heard=(feeds_map or {}).get(cs, []),
                    enriched_at=datetime.now(timezone.utc),
                    source="opensky-historical",
                )
                if flight.get("firstSeen"):
                    enrichment.first_seen_utc = datetime.fromtimestamp(
                        flight["firstSeen"], tz=timezone.utc
                    )
                if flight.get("lastSeen"):
                    enrichment.last_seen_utc = datetime.fromtimestamp(
                        flight["lastSeen"], tz=timezone.utc
                    )
                existing = self.store.get(cs)
                if existing:
                    enrichment.feeds_heard = list(
                        set((existing.feeds_heard or []) + enrichment.feeds_heard)
                    )
                self.store.upsert(enrichment)
                stats["matched"] += 1
            else:
                stats["not_found"] += 1

            if on_progress:
                on_progress(cs, flight)

        return stats

    def get_enrichment(self, callsign: str) -> Optional[FlightEnrichment]:
        """Get cached enrichment without hitting the API."""
        return self.store.get(callsign)

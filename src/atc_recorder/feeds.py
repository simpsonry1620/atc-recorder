"""Feed discovery and management for LiveATC.net."""

import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .config import Config


@dataclass
class Feed:
    """Represents a LiveATC feed."""

    feed_id: str
    title: str
    frequencies: list[dict]  # List of {"facility": str, "frequency": str}
    status: str  # "UP" or "DOWN"
    archive_url: str
    stream_pls_url: str

    @property
    def is_up(self) -> bool:
        """Check if the feed is currently online."""
        return self.status.upper() == "UP"

    @property
    def primary_frequency(self) -> Optional[str]:
        """Get the primary frequency for this feed."""
        if self.frequencies:
            return self.frequencies[0].get("frequency")
        return None

    def __str__(self) -> str:
        freq_str = self.primary_frequency or "N/A"
        status_indicator = "🟢" if self.is_up else "🔴"
        return f"{status_indicator} {self.feed_id}: {self.title} ({freq_str} MHz)"


class FeedDiscovery:
    """Discover and manage LiveATC feeds."""

    BASE_URL = "https://www.liveatc.net"
    SEARCH_URL = f"{BASE_URL}/search/"
    PLAY_URL = f"{BASE_URL}/play"

    def __init__(self, config: Optional[Config] = None):
        """Initialize the feed discovery.

        Args:
            config: Optional configuration object
        """
        self.config = config or Config()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
            }
        )

    def get_feeds(self, icao: str) -> list[Feed]:
        """Get all available feeds for an airport.

        Args:
            icao: ICAO airport code (e.g., 'kdca', 'kjfk')

        Returns:
            List of Feed objects
        """
        url = f"{self.SEARCH_URL}?icao={icao.lower()}"

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FeedDiscoveryError(f"Failed to fetch feeds for {icao}: {e}")

        return self._parse_feeds_page(response.text)

    def _parse_feeds_page(self, html: str) -> list[Feed]:
        """Parse the feeds page HTML.

        Args:
            html: HTML content of the search page

        Returns:
            List of Feed objects
        """
        soup = BeautifulSoup(html, "html.parser")
        feeds = []

        # Find all feed tables - they have class 'body' and contain feed info
        # The structure alternates between feed info tables and frequency tables
        tables = soup.find_all("table")

        current_feed_data = None

        for table in tables:
            # Check if this is a feed info table (contains status and links)
            strong = table.find("strong")
            if strong and table.find("a", href=lambda x: x and "archive.php" in x):
                # This is a feed info table
                title = strong.get_text(strip=True)

                # Get status
                font = table.find("font")
                status = font.get_text(strip=True) if font else "UNKNOWN"

                # Get archive link to extract feed_id
                archive_link = table.find("a", href=lambda x: x and "archive.php" in x)
                archive_href = archive_link.get("href", "") if archive_link else ""

                # Extract feed_id from archive URL
                feed_id_match = re.search(r"archive\.php\?m=([a-zA-Z0-9_]+)", archive_href)
                feed_id = feed_id_match.group(1) if feed_id_match else None

                # Get PLS stream link
                pls_link = table.find("a", href=lambda x: x and ".pls" in x)
                pls_href = pls_link.get("href", "") if pls_link else ""

                if feed_id:
                    current_feed_data = {
                        "feed_id": feed_id,
                        "title": title,
                        "status": status,
                        "archive_url": (
                            f"{self.BASE_URL}{archive_href}"
                            if archive_href.startswith("/")
                            else archive_href
                        ),
                        "stream_pls_url": (
                            f"{self.BASE_URL}{pls_href}" if pls_href.startswith("/") else pls_href
                        ),
                        "frequencies": [],
                    }

            # Check if this is a frequency table
            elif current_feed_data and table.find("td"):
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        facility = cells[0].get_text(strip=True)
                        frequency = cells[1].get_text(strip=True)

                        # Skip header rows
                        if facility.lower() == "facility" or not frequency:
                            continue

                        # Validate frequency format (should be numbers with decimal)
                        if re.match(r"^\d+\.\d+$", frequency):
                            current_feed_data["frequencies"].append(
                                {
                                    "facility": facility,
                                    "frequency": frequency,
                                }
                            )

                # Create the feed object if we have frequencies
                if current_feed_data.get("frequencies") or current_feed_data.get("feed_id"):
                    feeds.append(
                        Feed(
                            feed_id=current_feed_data["feed_id"],
                            title=current_feed_data["title"],
                            frequencies=current_feed_data.get("frequencies", []),
                            status=current_feed_data["status"],
                            archive_url=current_feed_data["archive_url"],
                            stream_pls_url=current_feed_data["stream_pls_url"],
                        )
                    )
                    current_feed_data = None

        # Deduplicate feeds by feed_id
        seen_ids = set()
        unique_feeds = []
        for feed in feeds:
            if feed.feed_id not in seen_ids:
                seen_ids.add(feed.feed_id)
                unique_feeds.append(feed)

        return unique_feeds

    def get_stream_url(self, feed_id: str) -> Optional[str]:
        """Get the actual stream URL for a feed.

        Args:
            feed_id: The feed identifier (e.g., 'kdca1_gnd')

        Returns:
            The stream URL or None if not found
        """
        pls_url = f"{self.PLAY_URL}/{feed_id}.pls"

        try:
            response = self.session.get(pls_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FeedDiscoveryError(f"Failed to fetch stream URL for {feed_id}: {e}")

        return self._parse_pls(response.text)

    def _parse_pls(self, pls_content: str) -> Optional[str]:
        """Parse a PLS playlist file to extract the stream URL.

        Args:
            pls_content: Content of the PLS file

        Returns:
            The stream URL or None if not found
        """
        # PLS format: File1=http://...
        for line in pls_content.splitlines():
            line = line.strip()
            if line.lower().startswith("file1="):
                return line.split("=", 1)[1].strip()

        return None

    def get_feed_by_id(self, feed_id: str, icao: Optional[str] = None) -> Optional[Feed]:
        """Get a specific feed by its ID.

        Args:
            feed_id: The feed identifier
            icao: Optional ICAO code to search (extracted from feed_id if not provided)

        Returns:
            Feed object or None if not found
        """
        # Try to extract ICAO from feed_id
        if icao is None:
            # Common patterns: kdca1_gnd, kmrb1_app_luray
            match = re.match(r"^([a-z]{4})", feed_id.lower())
            if match:
                icao = match.group(1)
            else:
                return None

        feeds = self.get_feeds(icao)
        for feed in feeds:
            if feed.feed_id.lower() == feed_id.lower():
                return feed

        return None


class FeedDiscoveryError(Exception):
    """Exception raised when feed discovery fails."""

    pass


def list_feeds(icao: str, config: Optional[Config] = None) -> list[Feed]:
    """Convenience function to list feeds for an airport.

    Args:
        icao: ICAO airport code
        config: Optional configuration

    Returns:
        List of Feed objects
    """
    discovery = FeedDiscovery(config)
    return discovery.get_feeds(icao)


def get_stream_url(feed_id: str, config: Optional[Config] = None) -> Optional[str]:
    """Convenience function to get a stream URL.

    Args:
        feed_id: Feed identifier
        config: Optional configuration

    Returns:
        Stream URL or None
    """
    discovery = FeedDiscovery(config)
    return discovery.get_stream_url(feed_id)

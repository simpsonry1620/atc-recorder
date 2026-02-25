"""Archive download functionality for LiveATC.net historical recordings."""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

import requests
from bs4 import BeautifulSoup

from .config import Config
from .utils import ensure_dir, get_date_string, get_liveatc_date_string


@dataclass
class ArchiveFile:
    """Represents an archive file available for download."""

    feed_id: str
    archive_id: str
    date: datetime
    time_slot: str  # e.g., "0000Z", "0030Z"
    url: str

    @property
    def filename(self) -> str:
        """Generate the local filename for this archive."""
        date_str = get_date_string(self.date)
        return f"{self.feed_id}_{date_str}_{self.time_slot}.mp3"


@dataclass
class DownloadResult:
    """Result of a download operation."""

    success: bool
    archive_file: ArchiveFile
    output_file: Optional[Path] = None
    size_bytes: int = 0
    error: Optional[str] = None


class ArchiveDownloader:
    """Download historical recordings from LiveATC.net archives."""

    BASE_URL = "https://www.liveatc.net"
    ARCHIVE_URL = f"{BASE_URL}/archive.php"
    ARCHIVE_DOWNLOAD_URL = "https://archive.liveatc.net"

    def __init__(self, config: Optional[Config] = None):
        """Initialize the archive downloader.

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

    def get_archive_info(self, feed_id: str) -> dict:
        """Get archive information for a feed.

        Args:
            feed_id: Feed identifier (e.g., 'kdca1_gnd')

        Returns:
            Dict with archive_id and folder information
        """
        url = f"{self.ARCHIVE_URL}?m={feed_id}"

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise ArchiveError(f"Failed to fetch archive info for {feed_id}: {e}")

        return self._parse_archive_page(response.text, feed_id)

    def _parse_archive_page(self, html: str, feed_id: str) -> dict:
        """Parse the archive page to extract archive information.

        Args:
            html: HTML content of the archive page
            feed_id: Feed identifier

        Returns:
            Dict with archive information
        """
        soup = BeautifulSoup(html, "html.parser")

        # Find the selected option which contains the archive ID
        select = soup.find("select", {"name": "m"})
        if select:
            selected = select.find("option", selected=True)
            if selected:
                archive_id = selected.get("value", feed_id)
            else:
                archive_id = feed_id
        else:
            archive_id = feed_id

        # Try to find the folder from any archive links on the page
        # Archive URLs typically look like: https://archive.liveatc.net/kdca/KDCA-...
        folder = feed_id.split("_")[0].lower()  # Default to first part of feed_id

        # Look for actual archive download links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "archive.liveatc.net" in href:
                # Extract folder from URL
                match = re.search(r"archive\.liveatc\.net/([^/]+)/", href)
                if match:
                    folder = match.group(1)
                    break

        return {
            "feed_id": feed_id,
            "archive_id": archive_id,
            "folder": folder,
        }

    def list_available_archives(
        self,
        feed_id: str,
        date: datetime,
    ) -> list[ArchiveFile]:
        """List available archive files for a specific date.

        LiveATC archives are typically in 30-minute segments.

        Args:
            feed_id: Feed identifier
            date: Date to list archives for

        Returns:
            List of ArchiveFile objects
        """
        archive_info = self.get_archive_info(feed_id)
        archive_id = archive_info["archive_id"]
        folder = archive_info["folder"]

        date_str = get_liveatc_date_string(date)

        # Generate all possible 30-minute time slots for the day
        archives = []
        for hour in range(24):
            for minute in [0, 30]:
                time_slot = f"{hour:02d}{minute:02d}Z"

                # Construct the archive URL
                # Format: https://archive.liveatc.net/{folder}/{ARCHIVE_ID}-{date}-{time}.mp3
                filename = f"{archive_id}-{date_str}-{time_slot}.mp3"
                url = f"{self.ARCHIVE_DOWNLOAD_URL}/{folder}/{filename}"

                archives.append(
                    ArchiveFile(
                        feed_id=feed_id,
                        archive_id=archive_id,
                        date=date.replace(hour=hour, minute=minute, second=0, microsecond=0),
                        time_slot=time_slot,
                        url=url,
                    )
                )

        return archives

    def download(
        self,
        archive_file: ArchiveFile,
        output_dir: Optional[Path] = None,
    ) -> DownloadResult:
        """Download a single archive file.

        Args:
            archive_file: ArchiveFile to download
            output_dir: Optional output directory

        Returns:
            DownloadResult object
        """
        if output_dir is None:
            output_dir = self.config.output_dir

        output_dir = Path(output_dir)

        # Create output directory structure
        date_str = get_date_string(archive_file.date)
        segment_dir = ensure_dir(output_dir / archive_file.feed_id / date_str)
        output_file = segment_dir / archive_file.filename

        # Skip if already downloaded
        if output_file.exists():
            return DownloadResult(
                success=True,
                archive_file=archive_file,
                output_file=output_file,
                size_bytes=output_file.stat().st_size,
            )

        try:
            response = self.session.get(archive_file.url, timeout=120, stream=True)

            if response.status_code == 404:
                return DownloadResult(
                    success=False,
                    archive_file=archive_file,
                    error="Archive file not found (404)",
                )

            response.raise_for_status()

            # Download the file
            size_bytes = 0
            with open(output_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    size_bytes += len(chunk)

            # Save metadata
            self._save_metadata(segment_dir, archive_file, output_file, size_bytes)

            return DownloadResult(
                success=True,
                archive_file=archive_file,
                output_file=output_file,
                size_bytes=size_bytes,
            )

        except requests.RequestException as e:
            # Clean up partial file
            if output_file.exists():
                output_file.unlink()

            return DownloadResult(
                success=False,
                archive_file=archive_file,
                error=str(e),
            )

    def download_date_range(
        self,
        feed_id: str,
        start_date: datetime,
        end_date: Optional[datetime] = None,
        start_hour: int = 0,
        hours: int = 24,
        output_dir: Optional[Path] = None,
    ) -> Generator[DownloadResult, None, None]:
        """Download archives for a date range.

        Args:
            feed_id: Feed identifier
            start_date: Start date
            end_date: End date (defaults to start_date for single day)
            start_hour: Starting hour (0-23)
            hours: Number of hours to download
            output_dir: Optional output directory

        Yields:
            DownloadResult objects for each file
        """
        if end_date is None:
            end_date = start_date

        current_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=0)

        while current_date <= end_date:
            archives = self.list_available_archives(feed_id, current_date)

            # Filter by hour range
            for archive in archives:
                archive_hour = archive.date.hour

                # Check if this archive is within our desired range
                if archive_hour < start_hour:
                    continue
                if archive_hour >= start_hour + hours:
                    continue

                # Add delay between requests
                time.sleep(self.config.request_delay)

                yield self.download(archive, output_dir)

            current_date += timedelta(days=1)

    def _save_metadata(
        self,
        directory: Path,
        archive_file: ArchiveFile,
        output_file: Path,
        size_bytes: int,
    ) -> None:
        """Save or update metadata file in directory.

        Args:
            directory: Directory containing recordings
            archive_file: Archive file information
            output_file: Path to downloaded file
            size_bytes: Size of downloaded file
        """
        metadata_file = directory / "metadata.json"

        # Load existing metadata if present
        existing = []
        if metadata_file.exists():
            try:
                with open(metadata_file, "r") as f:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = [existing]
            except (json.JSONDecodeError, IOError):
                existing = []

        # Create metadata entry
        metadata = {
            "feed_id": archive_file.feed_id,
            "archive_id": archive_file.archive_id,
            "file": output_file.name,
            "start_time": archive_file.date.isoformat(),
            "time_slot": archive_file.time_slot,
            "duration_seconds": 1800,  # 30 minutes
            "size_bytes": size_bytes,
            "source": "archive",
            "source_url": archive_file.url,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }

        # Check if already exists (by filename)
        existing = [m for m in existing if m.get("file") != output_file.name]
        existing.append(metadata)

        # Save updated metadata
        with open(metadata_file, "w") as f:
            json.dump(existing, f, indent=2)


class ArchiveError(Exception):
    """Exception raised when archive operations fail."""

    pass


def download_archives(
    feed_id: str,
    date: datetime,
    start_hour: int = 0,
    hours: int = 24,
    output_dir: Optional[Path] = None,
    config: Optional[Config] = None,
) -> list[DownloadResult]:
    """Convenience function to download archives.

    Args:
        feed_id: Feed identifier
        date: Date to download
        start_hour: Starting hour
        hours: Number of hours to download
        output_dir: Optional output directory
        config: Optional configuration

    Returns:
        List of DownloadResult objects
    """
    downloader = ArchiveDownloader(config)
    results = list(
        downloader.download_date_range(
            feed_id=feed_id,
            start_date=date,
            start_hour=start_hour,
            hours=hours,
            output_dir=output_dir,
        )
    )
    return results

"""Command-line interface for ATC Recorder."""

import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from . import __version__
from .archive import ArchiveDownloader, download_archives
from .config import Config, load_config, DCA_FEEDS
from .feeds import FeedDiscovery, FeedDiscoveryError, list_feeds
from .stream import StreamRecorder, record_feed
from .utils import check_ffmpeg, format_duration, parse_duration

# Optional transcription imports
try:
    from .transcribe import (
        WhisperClient,
        transcribe_file as do_transcribe_file,
        watch_and_transcribe,
        find_untranscribed_files,
        transcribe_all as do_transcribe_all,
        compare_preprocessing,
        AudioPreprocess,
        RIVA_AVAILABLE,
        WATCHDOG_AVAILABLE,
    )
    TRANSCRIPTION_AVAILABLE = RIVA_AVAILABLE
except ImportError:
    TRANSCRIPTION_AVAILABLE = False
    RIVA_AVAILABLE = False
    WATCHDOG_AVAILABLE = False


console = Console()


def _build_transcript_ingest_callback(cfg: Config):
    """Create callback for auto-ingesting transcripts when enabled."""
    rag = getattr(cfg, "rag", None)
    if not rag or not rag.enabled or not rag.ingest_on_transcribe:
        return None

    try:
        from .ingest import TranscriptIngestionService
        ingest_service = TranscriptIngestionService(cfg)
    except Exception as exc:
        console.print(f"[yellow]RAG ingestion disabled: {exc}[/yellow]")
        return None

    def _callback(transcript_path: Path) -> None:
        stats = ingest_service.ingest_transcript(transcript_path)
        if stats.errors > 0:
            console.print(f"[red]✗[/red] RAG ingest failed for {transcript_path.name}")
        else:
            console.print(f"[green]✓[/green] RAG ingest: {transcript_path.name} ({stats.docs_upserted} docs)")

    return _callback


@click.group()
@click.version_option(version=__version__)
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.pass_context
def cli(ctx: click.Context, config: Optional[Path]) -> None:
    """ATC Recorder - Record and download ATC audio from LiveATC.net."""
    ctx.ensure_object(dict)
    ctx.obj['config'] = load_config(config)


@cli.group()
def feeds() -> None:
    """Manage and discover ATC feeds."""
    pass


@feeds.command('list')
@click.argument('icao')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed information')
@click.pass_context
def feeds_list(ctx: click.Context, icao: str, verbose: bool) -> None:
    """List available feeds for an airport.
    
    ICAO is the airport code (e.g., kdca, kjfk, klax).
    """
    config = ctx.obj['config']
    
    with console.status(f"[bold blue]Fetching feeds for {icao.upper()}..."):
        try:
            feed_list = list_feeds(icao, config)
        except FeedDiscoveryError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
    
    if not feed_list:
        console.print(f"[yellow]No feeds found for {icao.upper()}[/yellow]")
        return
    
    table = Table(title=f"Available Feeds for {icao.upper()}")
    table.add_column("Feed ID", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Description", style="white")
    table.add_column("Frequency", style="yellow")
    
    if verbose:
        table.add_column("Archive URL", style="dim")
    
    for feed in feed_list:
        status = "[green]UP[/green]" if feed.is_up else "[red]DOWN[/red]"
        freq = feed.primary_frequency or "N/A"
        
        if verbose:
            table.add_row(
                feed.feed_id,
                status,
                feed.title,
                freq,
                feed.archive_url,
            )
        else:
            table.add_row(
                feed.feed_id,
                status,
                feed.title,
                freq,
            )
    
    console.print(table)
    console.print(f"\n[dim]Total: {len(feed_list)} feeds[/dim]")


@feeds.command('url')
@click.argument('feed_id')
@click.pass_context
def feeds_url(ctx: click.Context, feed_id: str) -> None:
    """Get the stream URL for a specific feed."""
    config = ctx.obj['config']
    discovery = FeedDiscovery(config)
    
    with console.status(f"[bold blue]Fetching stream URL for {feed_id}..."):
        try:
            url = discovery.get_stream_url(feed_id)
        except FeedDiscoveryError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
    
    if url:
        console.print(f"[green]Stream URL:[/green] {url}")
    else:
        console.print(f"[yellow]Could not find stream URL for {feed_id}[/yellow]")


@cli.command()
@click.argument('feed_id')
@click.option('--duration', '-d', default='30m',
              help='Recording duration (e.g., 30m, 2h, 1h30m)')
@click.option('--output', '-o', type=click.Path(path_type=Path),
              help='Output directory')
@click.pass_context
def record(ctx: click.Context, feed_id: str, duration: str, output: Optional[Path]) -> None:
    """Record a live stream.
    
    FEED_ID is the feed identifier (e.g., kdca1_gnd).
    """
    config = ctx.obj['config']
    
    # Check ffmpeg
    if not check_ffmpeg():
        console.print("[red]Error: ffmpeg is not installed or not found in PATH[/red]")
        console.print("[dim]Install ffmpeg: apt install ffmpeg (Linux) or brew install ffmpeg (macOS)[/dim]")
        sys.exit(1)
    
    try:
        duration_seconds = parse_duration(duration)
    except ValueError as e:
        console.print(f"[red]Error: Invalid duration format: {e}[/red]")
        sys.exit(1)
    
    output_dir = output or config.output_dir
    
    console.print(f"[bold]Recording {feed_id}[/bold]")
    console.print(f"  Duration: {format_duration(duration_seconds)}")
    console.print(f"  Output: {output_dir}")
    console.print(f"  Segment: {format_duration(config.segment_duration)}")
    console.print()
    
    recorder = StreamRecorder(config)
    
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        console.print("\n[yellow]Stopping recording...[/yellow]")
        recorder.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    
    num_segments = (duration_seconds + config.segment_duration - 1) // config.segment_duration
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Recording {feed_id}...", total=num_segments)
        
        def on_segment_complete(result):
            if result.success:
                progress.console.print(f"  [green]✓[/green] Saved: {result.output_file.name}")
            else:
                progress.console.print(f"  [red]✗[/red] Error: {result.error}")
            progress.advance(task)
        
        results = recorder.record(
            feed_id=feed_id,
            duration_seconds=duration_seconds,
            output_dir=output_dir,
            on_segment_complete=on_segment_complete,
        )
    
    # Summary
    success_count = sum(1 for r in results if r.success)
    console.print()
    console.print(f"[bold]Recording complete[/bold]")
    console.print(f"  Segments: {success_count}/{len(results)} successful")
    
    if success_count < len(results):
        sys.exit(1)


@cli.command('record-all')
@click.argument('icao')
@click.option('--duration', '-d', default='30m',
              help='Recording duration (e.g., 30m, 2h, 1h30m)')
@click.option('--output', '-o', type=click.Path(path_type=Path),
              help='Output directory')
@click.pass_context
def record_all(ctx: click.Context, icao: str, duration: str, output: Optional[Path]) -> None:
    """Record all available feeds for an airport.
    
    ICAO is the airport code (e.g., kdca, kjfk, klax).
    """
    config = ctx.obj['config']
    
    # Check ffmpeg
    if not check_ffmpeg():
        console.print("[red]Error: ffmpeg is not installed or not found in PATH[/red]")
        sys.exit(1)
    
    try:
        duration_seconds = parse_duration(duration)
    except ValueError as e:
        console.print(f"[red]Error: Invalid duration format: {e}[/red]")
        sys.exit(1)
    
    # Get available feeds
    with console.status(f"[bold blue]Fetching feeds for {icao.upper()}..."):
        try:
            feed_list = list_feeds(icao, config)
        except FeedDiscoveryError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
    
    # Filter to only online feeds
    online_feeds = [f for f in feed_list if f.is_up]
    
    if not online_feeds:
        console.print(f"[yellow]No online feeds found for {icao.upper()}[/yellow]")
        return
    
    console.print(f"[bold]Recording {len(online_feeds)} feeds for {icao.upper()}[/bold]")
    console.print(f"  Duration: {format_duration(duration_seconds)}")
    console.print()
    
    # Note: This is a simplified version that records sequentially
    # A production version could use asyncio or threading for parallel recording
    console.print("[yellow]Note: Recording feeds sequentially. For parallel recording, run multiple instances.[/yellow]")
    console.print()
    
    for feed in online_feeds:
        console.print(f"[bold cyan]{feed.feed_id}[/bold cyan] - {feed.title}")
        
        results = record_feed(
            feed_id=feed.feed_id,
            duration=duration,
            output_dir=output or config.output_dir,
            config=config,
        )
        
        success_count = sum(1 for r in results if r.success)
        if success_count == len(results):
            console.print(f"  [green]✓ Complete ({success_count} segments)[/green]")
        else:
            console.print(f"  [yellow]Partial ({success_count}/{len(results)} segments)[/yellow]")


@cli.command()
@click.argument('feed_id')
@click.option('--date', '-d', required=True,
              help='Date to download (YYYY-MM-DD)')
@click.option('--start-hour', '-s', type=int, default=0,
              help='Starting hour (0-23)')
@click.option('--hours', '-h', type=int, default=24,
              help='Number of hours to download')
@click.option('--output', '-o', type=click.Path(path_type=Path),
              help='Output directory')
@click.pass_context
def download(
    ctx: click.Context,
    feed_id: str,
    date: str,
    start_hour: int,
    hours: int,
    output: Optional[Path],
) -> None:
    """Download historical archives.
    
    FEED_ID is the feed identifier (e.g., kdca1_gnd).
    """
    config = ctx.obj['config']
    
    try:
        target_date = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        console.print("[red]Error: Invalid date format. Use YYYY-MM-DD[/red]")
        sys.exit(1)
    
    output_dir = output or config.output_dir
    
    console.print(f"[bold]Downloading archives for {feed_id}[/bold]")
    console.print(f"  Date: {date}")
    console.print(f"  Hours: {start_hour:02d}:00 - {start_hour + hours:02d}:00 UTC")
    console.print(f"  Output: {output_dir}")
    console.print()
    
    downloader = ArchiveDownloader(config)
    
    # Calculate number of 30-minute segments
    num_segments = hours * 2
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Downloading archives...", total=num_segments)
        
        success_count = 0
        skip_count = 0
        fail_count = 0
        
        for result in downloader.download_date_range(
            feed_id=feed_id,
            start_date=target_date,
            start_hour=start_hour,
            hours=hours,
            output_dir=output_dir,
        ):
            if result.success:
                if result.size_bytes > 0:
                    size_kb = result.size_bytes / 1024
                    progress.console.print(
                        f"  [green]✓[/green] {result.archive_file.time_slot} ({size_kb:.1f} KB)"
                    )
                    success_count += 1
                else:
                    progress.console.print(
                        f"  [dim]○[/dim] {result.archive_file.time_slot} (already exists)"
                    )
                    skip_count += 1
            else:
                if "404" in str(result.error):
                    progress.console.print(
                        f"  [dim]-[/dim] {result.archive_file.time_slot} (not available)"
                    )
                else:
                    progress.console.print(
                        f"  [red]✗[/red] {result.archive_file.time_slot}: {result.error}"
                    )
                fail_count += 1
            
            progress.advance(task)
    
    console.print()
    console.print(f"[bold]Download complete[/bold]")
    console.print(f"  Downloaded: {success_count}")
    console.print(f"  Skipped: {skip_count}")
    console.print(f"  Not available: {fail_count}")


@cli.command()
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.pass_context
def daemon(ctx: click.Context, config: Optional[Path]) -> None:
    """Run continuous recording based on configuration.
    
    Reads feeds from config file and records them continuously.
    """
    if config:
        cfg = load_config(config)
    else:
        cfg = ctx.obj['config']
    
    if not cfg.feeds:
        console.print("[red]Error: No feeds configured in config file[/red]")
        console.print("[dim]Add feeds to your config.yaml:[/dim]")
        console.print("[dim]feeds:[/dim]")
        console.print("[dim]  - kdca1_gnd[/dim]")
        console.print("[dim]  - kdca2_twr[/dim]")
        sys.exit(1)
    
    if not cfg.recording.enabled:
        console.print("[yellow]Recording is disabled in configuration[/yellow]")
        sys.exit(0)
    
    # Check ffmpeg
    if not check_ffmpeg():
        console.print("[red]Error: ffmpeg is not installed or not found in PATH[/red]")
        sys.exit(1)
    
    console.print("[bold]ATC Recorder Daemon[/bold]")
    console.print(f"  Feeds: {len(cfg.feeds)}")
    console.print(f"  Segment: {format_duration(cfg.segment_duration)}")
    console.print(f"  Output: {cfg.output_dir}")
    console.print()
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()
    
    recorders = {}
    stop_requested = False
    
    def signal_handler(sig, frame):
        nonlocal stop_requested
        console.print("\n[yellow]Stopping daemon...[/yellow]")
        stop_requested = True
        for recorder in recorders.values():
            recorder.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Simple sequential recording loop
    # A production version would use asyncio or threading
    while not stop_requested:
        for feed_id in cfg.feeds:
            if stop_requested:
                break
            
            console.print(f"[cyan]Recording: {feed_id}[/cyan]")
            
            recorder = StreamRecorder(cfg)
            recorders[feed_id] = recorder
            
            results = recorder.record(
                feed_id=feed_id,
                duration_seconds=cfg.segment_duration,
                output_dir=cfg.output_dir,
            )
            
            for result in results:
                if result.success:
                    console.print(f"  [green]✓[/green] {result.output_file.name}")
                else:
                    console.print(f"  [red]✗[/red] {result.error}")
    
    console.print("[bold]Daemon stopped[/bold]")


@cli.command('check')
@click.option('--quiet', '-q', is_flag=True, help='Only output on failure (for health checks)')
@click.option('--strict', is_flag=True, help='Fail on network issues (default: network is warning only)')
def check(quiet: bool, strict: bool) -> None:
    """Check system requirements.
    
    Returns exit code 0 if critical checks pass, 1 otherwise.
    Network connectivity is a warning by default (use --strict to make it critical).
    Use --quiet for Docker health checks.
    """
    critical_passed = True
    has_warnings = False
    
    if not quiet:
        console.print("[bold]System Check[/bold]")
        console.print()
    
    # Check ffmpeg (critical)
    if check_ffmpeg():
        if not quiet:
            console.print("[green]✓[/green] ffmpeg is installed")
    else:
        critical_passed = False
        if not quiet:
            console.print("[red]✗[/red] ffmpeg is not installed")
            console.print("  [dim]Install: apt install ffmpeg (Linux) or brew install ffmpeg (macOS)[/dim]")
        else:
            console.print("FAIL: ffmpeg not installed")
    
    # Check output directory is writable (critical)
    try:
        from pathlib import Path
        output_dir = Path("/app/recordings") if Path("/app/recordings").exists() else Path("./recordings")
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
        if not quiet:
            console.print(f"[green]✓[/green] Output directory is writable: {output_dir}")
    except Exception as e:
        critical_passed = False
        if not quiet:
            console.print(f"[red]✗[/red] Output directory not writable: {e}")
        else:
            console.print(f"FAIL: Output directory not writable")
    
    # Check network connectivity (warning only, unless --strict)
    try:
        import requests
        response = requests.get("https://www.liveatc.net", timeout=10)
        if response.status_code == 200:
            if not quiet:
                console.print("[green]✓[/green] LiveATC.net is accessible")
        else:
            has_warnings = True
            if strict:
                critical_passed = False
            if not quiet:
                console.print(f"[yellow]![/yellow] LiveATC.net returned status {response.status_code} (may be rate-limited)")
    except Exception as e:
        has_warnings = True
        if strict:
            critical_passed = False
        if not quiet:
            console.print(f"[yellow]![/yellow] Cannot reach LiveATC.net: {e}")
    
    if not quiet:
        console.print()
        if critical_passed and not has_warnings:
            console.print("[bold green]All checks passed![/bold green]")
        elif critical_passed:
            console.print("[bold yellow]Checks passed with warnings.[/bold yellow]")
        else:
            console.print("[bold red]Critical checks failed.[/bold red]")
    
    if not critical_passed:
        sys.exit(1)


@cli.command('transcribe')
@click.argument('audio_file', type=click.Path(path_type=Path), required=False)
@click.option('--host', '-H', envvar='WHISPER_GRPC_HOST', default='localhost',
              help='Whisper gRPC host (env: WHISPER_GRPC_HOST)')
@click.option('--port', '-p', envvar='WHISPER_GRPC_PORT', default=50051, type=int,
              help='Whisper gRPC port (env: WHISPER_GRPC_PORT)')
@click.option('--language', '-l', default='en-US',
              help='Language code (BCP-47 format)')
@click.option('--check', 'check_only', is_flag=True,
              help='Only check connection to Whisper service')
@click.option('--no-save', is_flag=True,
              help='Do not save transcript to file')
@click.option('--segment-by-pauses', is_flag=True,
              help='Segment by silence and timestamp each segment')
@click.option('--min-silence-duration', default=0.5, type=float,
              help='Min silence duration (seconds) for pause detection')
@click.option('--silence-db', default=-30.0, type=float,
              help='dB level below which audio is considered silence')
@click.option('--output-format', type=click.Choice(['json', 'timestamped-txt', 'srt']), default='json',
              help='Output format: json only, or also write timestamped .txt or .srt')
@click.option('--periodic-markers', default=0, type=float,
              help='In timestamped-txt, insert marker lines every N seconds (0=off)')
def transcribe(
    audio_file: Optional[Path],
    host: str,
    port: int,
    language: str,
    check_only: bool,
    no_save: bool,
    segment_by_pauses: bool,
    min_silence_duration: float,
    silence_db: float,
    output_format: str,
    periodic_markers: float,
) -> None:
    """Transcribe an audio file using Whisper ASR.
    
    AUDIO_FILE is the path to the audio file (MP3, WAV, etc.).
    
    Requires the NVIDIA Whisper ASR service to be running.
    """
    if not TRANSCRIPTION_AVAILABLE:
        console.print("[red]Error: Transcription dependencies not installed[/red]")
        console.print("[dim]Install with: pip install nvidia-riva-client[/dim]")
        sys.exit(1)
    
    if check_only:
        # Just check connection
        client = WhisperClient(grpc_host=host, grpc_port=port, language_code=language)
        with console.status(f"[bold blue]Checking connection to {host}:{port}..."):
            if client.check_connection():
                console.print(f"[green]✓[/green] Whisper service is available at {host}:{port}")
                sys.exit(0)
            else:
                console.print(f"[red]✗[/red] Cannot connect to Whisper service at {host}:{port}")
                sys.exit(1)
    
    # Audio file is required when not checking
    if audio_file is None:
        console.print("[red]Error: AUDIO_FILE is required[/red]")
        sys.exit(1)
    
    if not audio_file.exists():
        console.print(f"[red]Error: File not found: {audio_file}[/red]")
        sys.exit(1)
    
    console.print(f"[bold]Transcribing: {audio_file}[/bold]")
    console.print(f"  Service: {host}:{port}")
    console.print(f"  Language: {language}")
    console.print()
    
    with console.status("[bold blue]Transcribing..."):
        result = do_transcribe_file(
            audio_path=audio_file,
            grpc_host=host,
            grpc_port=port,
            language_code=language,
            save=not no_save,
            segment_by_pauses=segment_by_pauses,
            min_silence_duration=min_silence_duration,
            silence_threshold_dB=silence_db,
            output_format=output_format,
            periodic_timestamp_interval_sec=periodic_markers,
        )
    
    if result.success:
        console.print("[green]✓ Transcription complete[/green]")
        console.print()
        console.print("[bold]Transcript:[/bold]")
        console.print(result.text)
        console.print()
        
        if result.transcript_file and not no_save:
            console.print(f"[dim]Saved to: {result.transcript_file}[/dim]")
            if output_format == 'timestamped-txt':
                txt_path = result.audio_file.with_suffix('.txt')
                if txt_path.exists():
                    console.print(f"[dim]Timestamped text: {txt_path}[/dim]")
            elif output_format == 'srt':
                srt_path = result.audio_file.with_suffix('.srt')
                if srt_path.exists():
                    console.print(f"[dim]SRT: {srt_path}[/dim]")
    else:
        console.print(f"[red]✗ Transcription failed: {result.error}[/red]")
        sys.exit(1)


@cli.command('transcribe-watch')
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--host', '-H', envvar='WHISPER_GRPC_HOST', default='localhost',
              help='Whisper gRPC host (env: WHISPER_GRPC_HOST)')
@click.option('--port', '-p', envvar='WHISPER_GRPC_PORT', default=50051, type=int,
              help='Whisper gRPC port (env: WHISPER_GRPC_PORT)')
@click.option('--language', '-l', default='en-US',
              help='Language code (BCP-47 format)')
@click.pass_context
def transcribe_watch(
    ctx: click.Context,
    config: Optional[Path],
    host: str,
    port: int,
    language: str,
) -> None:
    """Watch for new recordings and transcribe them automatically.
    
    Monitors the recordings directory for new audio files and transcribes
    them using the NVIDIA Whisper ASR service.
    
    Requires the NVIDIA Whisper ASR service to be running.
    """
    if not TRANSCRIPTION_AVAILABLE:
        console.print("[red]Error: Transcription dependencies not installed[/red]")
        console.print("[dim]Install with: pip install nvidia-riva-client[/dim]")
        sys.exit(1)
    
    if not WATCHDOG_AVAILABLE:
        console.print("[red]Error: watchdog is not installed[/red]")
        console.print("[dim]Install with: pip install watchdog[/dim]")
        sys.exit(1)
    
    if config:
        cfg = load_config(config)
    else:
        cfg = ctx.obj['config']
    
    watch_dir = cfg.output_dir
    
    # Transcription options from config
    trans = getattr(cfg, 'transcription', None)
    segment_by_pauses = trans.segment_by_pauses if trans else False
    min_silence_duration = trans.min_silence_duration if trans else 0.5
    silence_db = trans.silence_threshold_dB if trans else -30.0
    min_speech_duration = trans.min_speech_duration if trans else 0.3
    merge_gap_seconds = trans.merge_gap_seconds if trans else 0.5
    output_format = trans.output_format if trans else 'json'

    console.print("[bold]ATC Transcription Watcher[/bold]")
    console.print(f"  Watch directory: {watch_dir}")
    console.print(f"  Whisper service: {host}:{port}")
    console.print(f"  Language: {language}")
    if segment_by_pauses:
        console.print(f"  Segment by pauses: yes (min_silence={min_silence_duration}s)")
    console.print(f"  Output format: {output_format}")
    console.print()
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()

    try:
        ingest_callback = _build_transcript_ingest_callback(cfg)
        watch_and_transcribe(
            watch_dir=watch_dir,
            grpc_host=host,
            grpc_port=port,
            language_code=language,
            segment_by_pauses=segment_by_pauses,
            min_silence_duration=min_silence_duration,
            silence_threshold_dB=silence_db,
            min_speech_duration=min_speech_duration,
            merge_gap_seconds=merge_gap_seconds,
            output_format=output_format,
            on_transcript_saved=ingest_callback,
        )
    except KeyboardInterrupt:
        pass
    
    console.print("[bold]Watcher stopped[/bold]")


@cli.command('transcribe-all')
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--host', '-H', envvar='WHISPER_GRPC_HOST', default='localhost',
              help='Whisper gRPC host (env: WHISPER_GRPC_HOST)')
@click.option('--port', '-p', envvar='WHISPER_GRPC_PORT', default=50051, type=int,
              help='Whisper gRPC port (env: WHISPER_GRPC_PORT)')
@click.option('--language', '-l', default='en-US',
              help='Language code (BCP-47 format)')
@click.option('--force', '-f', is_flag=True,
              help='Re-transcribe all audio files (overwrite existing transcripts)')
@click.option('--segment-by-pauses', is_flag=True,
              help='Segment by silence and timestamp each segment')
@click.option('--output-format', type=click.Choice(['json', 'timestamped-txt', 'srt']), default='json',
              help='Output format: json only, or also write .txt or .srt')
@click.option('--dry-run', is_flag=True,
              help='Show files that would be transcribed without processing')
@click.pass_context
def transcribe_all(
    ctx: click.Context,
    config: Optional[Path],
    host: str,
    port: int,
    language: str,
    force: bool,
    segment_by_pauses: bool,
    output_format: str,
    dry_run: bool,
) -> None:
    """Transcribe recordings in the recordings directory.
    
    By default only transcribes MP3 files that don't have a transcript yet.
    Use --force to re-transcribe all audio files (overwrites existing JSON).
    
    Requires the NVIDIA Whisper ASR service to be running.
    """
    if not TRANSCRIPTION_AVAILABLE:
        console.print("[red]Error: Transcription dependencies not installed[/red]")
        console.print("[dim]Install with: pip install nvidia-riva-client[/dim]")
        sys.exit(1)
    
    if config:
        cfg = load_config(config)
    else:
        cfg = ctx.obj['config']
    
    recordings_dir = cfg.output_dir
    
    # Find files to transcribe
    with console.status("[bold blue]Scanning for files..."):
        if force:
            from .transcribe import find_audio_files
            files = find_audio_files(recordings_dir)
        else:
            files = find_untranscribed_files(recordings_dir)
    
    if not files:
        console.print("[green]No files to transcribe.[/green]" if force else "[green]All recordings have been transcribed![/green]")
        return
    
    console.print(f"[bold]Found {len(files)} files to transcribe[/bold]" + (" (force)" if force else ""))
    console.print()
    
    if dry_run:
        console.print("[yellow]Dry run - files that would be transcribed:[/yellow]")
        for f in files:
            console.print(f"  {f}")
        return
    
    console.print(f"  Whisper service: {host}:{port}")
    console.print(f"  Language: {language}")
    if segment_by_pauses:
        console.print("  Segment by pauses: yes")
    console.print(f"  Output format: {output_format}")
    console.print()

    # Check connection first
    client = WhisperClient(grpc_host=host, grpc_port=port, language_code=language)
    with console.status("[bold blue]Connecting to Whisper service..."):
        if not client.check_connection():
            console.print(f"[red]✗ Cannot connect to Whisper service at {host}:{port}[/red]")
            sys.exit(1)

    console.print("[green]✓[/green] Connected to Whisper service")
    console.print()

    success_count = 0
    fail_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Transcribing...", total=len(files))

        for audio_file in files:
            progress.update(task, description=f"Transcribing {audio_file.name}...")

            try:
                result = client.convert_and_transcribe(
                    audio_file,
                    segment_by_pauses=segment_by_pauses,
                )

                if result.success:
                    from .transcribe import save_transcript, export_timestamped_txt, export_srt
                    save_transcript(result)
                    if output_format == "timestamped-txt":
                        export_timestamped_txt(result)
                    elif output_format == "srt":
                        export_srt(result)
                    progress.console.print(f"  [green]✓[/green] {audio_file.name}")
                    success_count += 1
                else:
                    progress.console.print(f"  [red]✗[/red] {audio_file.name}: {result.error}")
                    fail_count += 1
                    
            except Exception as e:
                progress.console.print(f"  [red]✗[/red] {audio_file.name}: {e}")
                fail_count += 1
            
            progress.advance(task)
    
    console.print()
    console.print("[bold]Batch transcription complete[/bold]")
    console.print(f"  Success: {success_count}")
    console.print(f"  Failed: {fail_count}")
    
    if fail_count > 0:
        sys.exit(1)


@cli.command('transcribe-compare')
@click.argument('audio_file', type=click.Path(exists=True, path_type=Path))
@click.option('--host', '-H', envvar='WHISPER_GRPC_HOST', default='localhost',
              help='Whisper gRPC host (env: WHISPER_GRPC_HOST)')
@click.option('--port', '-p', envvar='WHISPER_GRPC_PORT', default=50051, type=int,
              help='Whisper gRPC port (env: WHISPER_GRPC_PORT)')
@click.option('--language', '-l', default='en-US',
              help='Language code (BCP-47 format)')
@click.option('--output', '-o', type=click.Path(path_type=Path),
              help='Output directory for comparison results')
def transcribe_compare(
    audio_file: Path,
    host: str,
    port: int,
    language: str,
    output: Optional[Path],
) -> None:
    """Compare transcription with different audio preprocessing methods.
    
    Transcribes the same AUDIO_FILE using three preprocessing methods:
    - none: No preprocessing (raw audio)
    - ffmpeg: FFmpeg filters (bandpass + noise reduction + normalization)
    - sox: Sox noise reduction with automatic noise profiling
    
    Saves separate transcript files for each method to compare accuracy.
    
    Requires the NVIDIA Whisper ASR service to be running.
    """
    if not TRANSCRIPTION_AVAILABLE:
        console.print("[red]Error: Transcription dependencies not installed[/red]")
        console.print("[dim]Install with: pip install nvidia-riva-client[/dim]")
        sys.exit(1)
    
    console.print("[bold]Preprocessing Comparison Test[/bold]")
    console.print(f"  Audio file: {audio_file}")
    console.print(f"  Whisper service: {host}:{port}")
    console.print(f"  Language: {language}")
    console.print()
    
    import shutil
    if not shutil.which("sox"):
        console.print("[yellow]Note: sox not found - sox preprocessing will be skipped[/yellow]")
        console.print("[dim]Install sox: apt install sox[/dim]")
        console.print()
    
    results = compare_preprocessing(
        audio_path=audio_file,
        grpc_host=host,
        grpc_port=port,
        language_code=language,
        output_dir=output,
    )
    
    if not results:
        console.print("[red]Comparison failed - could not connect to Whisper service[/red]")
        sys.exit(1)
    
    # Print final summary
    console.print()
    console.print("[bold]Results saved:[/bold]")
    for method_name, result in results.items():
        if result.success and result.transcript_file:
            console.print(f"  [green]✓[/green] {result.transcript_file.name}")
        else:
            console.print(f"  [red]✗[/red] {method_name}: {result.error}")
    
    console.print()
    console.print("[dim]Compare the transcript files to see which preprocessing works best.[/dim]")


@cli.command("ingest-transcripts")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--recordings-dir', type=click.Path(path_type=Path),
              help='Directory to scan for transcript JSON files')
@click.pass_context
def ingest_transcripts(
    ctx: click.Context,
    config: Optional[Path],
    recordings_dir: Optional[Path],
) -> None:
    """Backfill transcript JSON files into vector + metadata stores."""
    cfg = load_config(config) if config else ctx.obj['config']
    if not getattr(cfg, "rag", None) or not cfg.rag.enabled:
        console.print("[red]Error: rag.enabled is false or missing in config[/red]")
        sys.exit(1)

    try:
        from .ingest import TranscriptIngestionService
        service = TranscriptIngestionService(cfg)
    except Exception as exc:
        console.print(f"[red]Error initializing ingestion service: {exc}[/red]")
        sys.exit(1)

    target_dir = recordings_dir or cfg.output_dir
    console.print(f"[bold]Backfilling transcripts[/bold]")
    console.print(f"  Source: {target_dir}")
    with console.status("[bold blue]Indexing transcripts..."):
        stats = service.backfill(target_dir)

    console.print("[green]✓ Backfill complete[/green]")
    console.print(f"  Files processed: {stats.files_processed}")
    console.print(f"  Docs upserted: {stats.docs_upserted}")
    console.print(f"  Docs skipped: {stats.docs_skipped}")
    console.print(f"  Errors: {stats.errors}")
    if stats.errors > 0:
        sys.exit(1)


@cli.command("search")
@click.argument("query")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--start-time', help='Filter start time (ISO-8601 UTC)')
@click.option('--end-time', help='Filter end time (ISO-8601 UTC)')
@click.option('--feed-id', 'feed_ids', multiple=True, help='Include only these feed IDs')
@click.option('--exclude-feed-id', 'exclude_feed_ids', multiple=True, help='Exclude feed IDs')
@click.option('--top-k', default=10, type=int, help='Max number of results')
@click.pass_context
def search_cmd(
    ctx: click.Context,
    query: str,
    config: Optional[Path],
    start_time: Optional[str],
    end_time: Optional[str],
    feed_ids: tuple[str, ...],
    exclude_feed_ids: tuple[str, ...],
    top_k: int,
) -> None:
    """Semantic transcript search with optional time/channel filters."""
    cfg = load_config(config) if config else ctx.obj['config']
    if not getattr(cfg, "rag", None) or not cfg.rag.enabled:
        console.print("[red]Error: rag.enabled is false or missing in config[/red]")
        sys.exit(1)

    try:
        from .ingest import TranscriptIngestionService
        from .rag_models import SearchFilters
        service = TranscriptIngestionService(cfg)
        filters = SearchFilters(
            start_time_utc=datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
            if start_time else None,
            end_time_utc=datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
            if end_time else None,
            feed_ids=list(feed_ids) if feed_ids else None,
            exclude_feed_ids=list(exclude_feed_ids) if exclude_feed_ids else None,
        )
        hits = service.search(query=query, filters=filters, top_k=top_k)
    except Exception as exc:
        console.print(f"[red]Search failed: {exc}[/red]")
        sys.exit(1)

    if not hits:
        console.print("[yellow]No results.[/yellow]")
        return

    table = Table(title=f"Search Results ({len(hits)})")
    table.add_column("Score", style="green")
    table.add_column("Feed", style="cyan")
    table.add_column("Start", style="yellow")
    table.add_column("Text", style="white")
    for hit in hits:
        snippet = (hit.text[:120] + "...") if len(hit.text) > 120 else hit.text
        table.add_row(
            f"{hit.score:.4f}",
            hit.feed_id,
            hit.start_time_utc.isoformat(),
            snippet,
        )
    console.print(table)


@cli.command("rag-check")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.pass_context
def rag_check(ctx: click.Context, config: Optional[Path]) -> None:
    """Check embedding and vector store connectivity."""
    cfg = load_config(config) if config else ctx.obj['config']
    if not getattr(cfg, "rag", None) or not cfg.rag.enabled:
        console.print("[red]Error: rag.enabled is false or missing in config[/red]")
        sys.exit(1)

    try:
        from .embedding import create_embedding_client
        from .vector_store import create_vector_store
        embedding = create_embedding_client(cfg.rag.embedding)
        vector_store = create_vector_store(cfg.rag.vector_store)
    except Exception as exc:
        console.print(f"[red]RAG init failed: {exc}[/red]")
        sys.exit(1)

    ok = True
    if embedding.check_health():
        console.print("[green]✓[/green] Embedding endpoint reachable")
    else:
        ok = False
        console.print("[red]✗[/red] Embedding endpoint unavailable")

    if vector_store.check_health():
        console.print("[green]✓[/green] Vector store reachable")
    else:
        ok = False
        console.print("[red]✗[/red] Vector store unavailable")

    if not ok:
        sys.exit(1)


@cli.command("rag-api")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.pass_context
def rag_api(ctx: click.Context, config: Optional[Path]) -> None:
    """Run HTTP search API server."""
    cfg = load_config(config) if config else ctx.obj['config']
    if not getattr(cfg, "rag", None) or not cfg.rag.enabled:
        console.print("[red]Error: rag.enabled is false or missing in config[/red]")
        sys.exit(1)
    try:
        from .search_api import SearchApiServer
        SearchApiServer(cfg).run()
    except Exception as exc:
        console.print(f"[red]Failed to start API server: {exc}[/red]")
        sys.exit(1)


if __name__ == '__main__':
    cli()

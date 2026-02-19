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


def _resolve_preprocess(method: Optional[str]) -> "AudioPreprocess":
    """Convert preprocess option/config string to AudioPreprocess enum."""
    raw = (method or "none").strip().lower()
    try:
        return AudioPreprocess(raw)
    except ValueError:
        valid = ", ".join(p.value for p in AudioPreprocess)
        raise click.ClickException(
            f"Invalid preprocess mode '{method}'. Expected one of: {valid}."
        )


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


def _build_variant_store(cfg: Config):
    """Create a TranscriptVariantStore from the config, or None on failure."""
    trans = getattr(cfg, "transcription", None)
    db_path = Path(trans.variant_store_path) if trans else Path("./recordings/transcripts.db")
    try:
        from .variant_store import TranscriptVariantStore
        return TranscriptVariantStore(db_path=db_path, recordings_root=cfg.output_dir)
    except Exception as exc:
        console.print(f"[yellow]Variant store unavailable: {exc}[/yellow]")
        return None


def _get_asr_model_name(host: str, port: int) -> str:
    """Infer ASR model name from the gRPC endpoint environment/defaults."""
    import os
    grpc_host = os.environ.get("WHISPER_GRPC_HOST", host)
    if "parakeet" in grpc_host.lower():
        return "parakeet-tdt-0.6b-v2"
    if port == 50052:
        return "parakeet-tdt-0.6b-v2"
    return "whisper-large-v3"


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
@click.option('--preprocess', type=click.Choice(['none', 'ffmpeg', 'ffmpeg_vad', 'sox']), default=None,
              help='Audio preprocessing mode (default: from config, fallback none)')
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
@click.option('--diarization', is_flag=True,
              help='Annotate segments with role diarization labels (ATC/PILOT/UNKNOWN)')
@click.option('--stitch-across-files', is_flag=True,
              help='Stitch boundary transmissions with previous transcript when adjacent in time')
@click.pass_context
def transcribe(
    ctx: click.Context,
    audio_file: Optional[Path],
    host: str,
    port: int,
    language: str,
    check_only: bool,
    no_save: bool,
    preprocess: Optional[str],
    segment_by_pauses: bool,
    min_silence_duration: float,
    silence_db: float,
    output_format: str,
    periodic_markers: float,
    diarization: bool,
    stitch_across_files: bool,
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

    cfg = ctx.obj['config']
    trans_cfg = getattr(cfg, "transcription", None)
    preprocess_value = preprocess if preprocess is not None else (trans_cfg.preprocess if trans_cfg else "none")
    preprocess_mode = _resolve_preprocess(preprocess_value)
    
    console.print(f"[bold]Transcribing: {audio_file}[/bold]")
    console.print(f"  Service: {host}:{port}")
    console.print(f"  Language: {language}")
    console.print(f"  Preprocess: {preprocess_mode.value}")
    console.print()
    
    with console.status("[bold blue]Transcribing..."):
        result = do_transcribe_file(
            audio_path=audio_file,
            grpc_host=host,
            grpc_port=port,
            language_code=language,
            save=not no_save,
            preprocess=preprocess_mode,
            segment_by_pauses=segment_by_pauses,
            min_silence_duration=min_silence_duration,
            silence_threshold_dB=silence_db,
            output_format=output_format,
            periodic_timestamp_interval_sec=periodic_markers,
            diarization_enabled=diarization,
            stitch_across_files=stitch_across_files,
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
@click.option('--preprocess', type=click.Choice(['none', 'ffmpeg', 'ffmpeg_vad', 'sox']), default=None,
              help='Audio preprocessing mode (default: from config, fallback none)')
@click.pass_context
def transcribe_watch(
    ctx: click.Context,
    config: Optional[Path],
    host: str,
    port: int,
    language: str,
    preprocess: Optional[str],
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
    preprocess_value = preprocess if preprocess is not None else (trans.preprocess if trans else "none")
    preprocess_mode = _resolve_preprocess(preprocess_value)
    segment_by_pauses = trans.segment_by_pauses if trans else False
    min_silence_duration = trans.min_silence_duration if trans else 0.5
    silence_db = trans.silence_threshold_dB if trans else -30.0
    min_speech_duration = trans.min_speech_duration if trans else 0.3
    merge_gap_seconds = trans.merge_gap_seconds if trans else 0.5
    output_format = trans.output_format if trans else 'json'
    diarization_enabled = trans.diarization_enabled if trans else False
    diarization_mode = trans.diarization_mode if trans else "role-heuristic"
    stitch_across_files = trans.stitch_across_files if trans else False
    stitch_max_gap_seconds = trans.stitch_max_gap_seconds if trans else 2.0
    stitch_min_text_overlap_chars = trans.stitch_min_text_overlap_chars if trans else 12

    console.print("[bold]ATC Transcription Watcher[/bold]")
    console.print(f"  Watch directory: {watch_dir}")
    console.print(f"  Whisper service: {host}:{port}")
    console.print(f"  Language: {language}")
    console.print(f"  Preprocess: {preprocess_mode.value}")
    if segment_by_pauses:
        console.print(f"  Segment by pauses: yes (min_silence={min_silence_duration}s)")
    console.print(f"  Role diarization: {'enabled' if diarization_enabled else 'disabled'}")
    console.print(f"  Cross-file stitching: {'enabled' if stitch_across_files else 'disabled'}")
    console.print(f"  Output format: {output_format}")
    console.print()
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()

    try:
        ingest_callback = _build_transcript_ingest_callback(cfg)
        vs = _build_variant_store(cfg)
        model_name = _get_asr_model_name(host, port)
        watch_and_transcribe(
            watch_dir=watch_dir,
            grpc_host=host,
            grpc_port=port,
            language_code=language,
            preprocess=preprocess_mode,
            segment_by_pauses=segment_by_pauses,
            min_silence_duration=min_silence_duration,
            silence_threshold_dB=silence_db,
            min_speech_duration=min_speech_duration,
            merge_gap_seconds=merge_gap_seconds,
            output_format=output_format,
            on_transcript_saved=ingest_callback,
            diarization_enabled=diarization_enabled,
            diarization_mode=diarization_mode,
            stitch_across_files=stitch_across_files,
            stitch_max_gap_seconds=stitch_max_gap_seconds,
            stitch_min_text_overlap_chars=stitch_min_text_overlap_chars,
            asr_model=model_name,
            variant_store=vs,
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
@click.option('--preprocess', type=click.Choice(['none', 'ffmpeg', 'ffmpeg_vad', 'sox']), default=None,
              help='Audio preprocessing mode (default: from config, fallback none)')
@click.option('--segment-by-pauses', is_flag=True,
              help='Segment by silence and timestamp each segment')
@click.option('--output-format', type=click.Choice(['json', 'timestamped-txt', 'srt']), default='json',
              help='Output format: json only, or also write .txt or .srt')
@click.option('--diarization', is_flag=True,
              help='Annotate segments with role diarization labels (ATC/PILOT/UNKNOWN)')
@click.option('--stitch-across-files', is_flag=True,
              help='Stitch boundary transmissions with previous transcript when adjacent in time')
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
    preprocess: Optional[str],
    segment_by_pauses: bool,
    output_format: str,
    diarization: bool,
    stitch_across_files: bool,
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
    trans = getattr(cfg, "transcription", None)
    preprocess_value = preprocess if preprocess is not None else (trans.preprocess if trans else "none")
    preprocess_mode = _resolve_preprocess(preprocess_value)
    diarization_enabled = diarization or (trans.diarization_enabled if trans else False)
    diarization_mode = trans.diarization_mode if trans else "role-heuristic"
    stitch_enabled = stitch_across_files or (trans.stitch_across_files if trans else False)
    stitch_max_gap_seconds = trans.stitch_max_gap_seconds if trans else 2.0
    stitch_min_text_overlap_chars = trans.stitch_min_text_overlap_chars if trans else 12
    
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
    console.print(f"  Preprocess: {preprocess_mode.value}")
    if segment_by_pauses:
        console.print("  Segment by pauses: yes")
    console.print(f"  Role diarization: {'enabled' if diarization_enabled else 'disabled'}")
    console.print(f"  Cross-file stitching: {'enabled' if stitch_enabled else 'disabled'}")
    console.print(f"  Output format: {output_format}")
    console.print()

    vs_batch = _build_variant_store(cfg)
    model_name_batch = _get_asr_model_name(host, port)

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
                    preprocess=preprocess_mode,
                    segment_by_pauses=segment_by_pauses,
                    diarization_enabled=diarization_enabled,
                    diarization_mode=diarization_mode,
                )

                if result.success:
                    from .transcribe import (
                        save_transcript,
                        export_timestamped_txt,
                        export_srt,
                        stitch_transcript_boundary_with_previous,
                        refresh_result_from_saved_transcript,
                    )
                    save_transcript(
                        result,
                        asr_model=model_name_batch,
                        preprocess=preprocess_mode.value,
                        variant_store=vs_batch,
                        recordings_root=recordings_dir,
                    )
                    if result.transcript_file and stitch_enabled:
                        stitched = stitch_transcript_boundary_with_previous(
                            result.transcript_file,
                            max_gap_seconds=stitch_max_gap_seconds,
                            min_text_overlap_chars=stitch_min_text_overlap_chars,
                        )
                        if stitched:
                            refresh_result_from_saved_transcript(result)
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
@click.pass_context
def transcribe_compare(
    ctx: click.Context,
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
    
    cfg = ctx.obj['config']
    vs = _build_variant_store(cfg)
    model_name = _get_asr_model_name(host, port)

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
        asr_model=model_name,
        variant_store=vs,
        recordings_root=cfg.output_dir,
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
    probe_vector_dim: int | None = None
    try:
        probe = embedding.embed_text("radio check")
        probe_vector_dim = len(probe.vector)
        console.print(
            f"[green]✓[/green] Embedding endpoint reachable "
            f"(model={probe.model}, dim={probe_vector_dim})"
        )
    except Exception as exc:
        ok = False
        console.print(f"[red]✗[/red] Embedding endpoint unavailable: {exc}")

    if vector_store.check_health():
        console.print("[green]✓[/green] Vector store reachable")
    else:
        ok = False
        console.print("[red]✗[/red] Vector store unavailable")

    expected_dim = cfg.rag.vector_store.embedding_dim
    if probe_vector_dim is not None:
        if probe_vector_dim == expected_dim:
            console.print(
                f"[green]✓[/green] Embedding dimension matches vector store config ({expected_dim})"
            )
        else:
            ok = False
            console.print(
                "[red]✗[/red] Embedding dimension mismatch: "
                f"endpoint returned {probe_vector_dim}, "
                f"but rag.vector_store.embedding_dim is {expected_dim}"
            )

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


@cli.command("dashboard")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--host', '-H', default='0.0.0.0', help='Dashboard bind address')
@click.option('--port', '-p', default=8050, type=int, help='Dashboard port')
@click.pass_context
def dashboard(ctx: click.Context, config: Optional[Path], host: str, port: int) -> None:
    """Run the web dashboard for visualizing recordings and transcripts."""
    cfg = load_config(config) if config else ctx.obj['config']
    console.print(f"[bold]ATC Recorder Dashboard[/bold]")
    console.print(f"  URL: http://{host}:{port}")
    console.print(f"  Recordings: {cfg.output_dir}")
    console.print()
    try:
        from .dashboard import run_dashboard
        run_dashboard(cfg, host=host, port=port)
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped[/yellow]")
    except Exception as exc:
        console.print(f"[red]Failed to start dashboard: {exc}[/red]")
        sys.exit(1)


@cli.command("extract-entities")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--recordings-dir', type=click.Path(path_type=Path),
              help='Directory to scan for transcript JSON files')
@click.pass_context
def extract_entities_cmd(
    ctx: click.Context,
    config: Optional[Path],
    recordings_dir: Optional[Path],
) -> None:
    """Backfill entity extraction on existing transcripts.

    Scans transcript JSON files and extracts callsigns, runways, altitudes,
    and frequencies into the entity_mentions table. Does not require RAG
    services — only needs the SQLite metadata database.
    """
    cfg = load_config(config) if config else ctx.obj['config']

    # Entity extraction needs the metadata store but not the full RAG stack
    if cfg.rag and cfg.rag.vector_store:
        db_path = Path(cfg.rag.vector_store.sqlite_metadata_path)
    else:
        db_path = cfg.output_dir / "rag_metadata.db"

    from .ingest import MetadataStore
    store = MetadataStore(db_path)
    store.ensure_schema()

    target_dir = recordings_dir or cfg.output_dir
    console.print("[bold]Entity Extraction Backfill[/bold]")
    console.print(f"  Source: {target_dir}")
    console.print(f"  Database: {db_path}")
    console.print()

    import json as _json
    from .entities import extract_entities

    files_processed = 0
    entities_found = 0
    errors = 0

    json_files = sorted(Path(target_dir).rglob("*.json"))
    json_files = [f for f in json_files if f.name != "metadata.json"]

    if not json_files:
        console.print("[yellow]No transcript JSON files found.[/yellow]")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Extracting entities...", total=len(json_files))

        for jf in json_files:
            try:
                data = _json.loads(jf.read_text(encoding="utf-8"))
                audio_file = data.get("audio_file", jf.with_suffix(".mp3").name)

                # Parse feed_id from filename
                feed_id = audio_file.split("_", 1)[0] if "_" in audio_file else "unknown"

                segments = data.get("segments", [])
                for idx, seg in enumerate(segments):
                    text = (seg.get("stitched_canonical_text") or seg.get("text") or "").strip()
                    if not text or text in {"...", "-"}:
                        continue

                    entities = extract_entities(text)
                    if entities:
                        import hashlib
                        start_s = float(seg.get("start_time", 0.0))
                        end_s = float(seg.get("end_time", start_s))
                        raw = f"{audio_file}:{idx}:{start_s:.3f}:{end_s:.3f}"
                        doc_id = f"seg_{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"

                        # Infer timestamp from filename
                        pieces = Path(audio_file).stem.split("_")
                        ts = ""
                        if len(pieces) >= 3:
                            ts = f"{pieces[-2]}T{pieces[-1].rstrip('Z')}:00Z" if len(pieces[-1]) == 4 else ""

                        store.upsert_entity_mentions(doc_id, feed_id, ts, entities)
                        entities_found += len(entities)

                files_processed += 1
            except Exception as exc:
                progress.console.print(f"  [red]✗[/red] {jf.name}: {exc}")
                errors += 1
            progress.advance(task)

    console.print()
    console.print("[green]✓ Entity extraction complete[/green]")
    console.print(f"  Files processed: {files_processed}")
    console.print(f"  Entities found: {entities_found}")
    console.print(f"  Errors: {errors}")

    if errors > 0:
        sys.exit(1)


@cli.command("enrich-flights")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--limit', '-n', default=50, type=int,
              help='Max callsigns to enrich')
@click.option('--historical', is_flag=True, default=False,
              help='Use historical airport arrival/departure data instead of live state vectors')
@click.option('--airport', default='KDCA',
              help='ICAO airport code for historical lookups (default: KDCA)')
@click.pass_context
def enrich_flights_cmd(
    ctx: click.Context,
    config: Optional[Path],
    limit: int,
    historical: bool,
    airport: str,
) -> None:
    """Batch-enrich extracted callsigns via OpenSky Network.

    By default, looks up callsigns against live state vectors in the
    configured bounding box. Use --historical to match against airport
    arrival/departure records for the time range of your recordings.
    """
    cfg = load_config(config) if config else ctx.obj['config']
    tracking = getattr(cfg, "tracking", None)

    if cfg.rag and cfg.rag.vector_store:
        db_path = Path(cfg.rag.vector_store.sqlite_metadata_path)
    else:
        db_path = cfg.output_dir / "rag_metadata.db"

    if not db_path.exists():
        console.print("[red]Error: metadata database not found. Run entity extraction first.[/red]")
        sys.exit(1)

    from .ingest import MetadataStore
    store = MetadataStore(db_path)
    store.ensure_schema()

    enrichment_db = Path(tracking.enrichment_db_path) if tracking else cfg.output_dir / "flight_enrichment.db"
    osky = tracking.opensky if tracking else None
    cache_ttl = osky.cache_ttl_seconds if osky else 3600
    creds_file = Path(osky.credentials_file) if osky else Path("./credentials.json")
    bbox = {
        "lamin": osky.bbox_lamin, "lamax": osky.bbox_lamax,
        "lomin": osky.bbox_lomin, "lomax": osky.bbox_lomax,
    } if osky else None

    from .opensky import OpenSkyEnrichmentService
    service = OpenSkyEnrichmentService(
        db_path=enrichment_db, credentials_file=creds_file,
        cache_ttl=cache_ttl, bbox=bbox,
    )
    has_creds = creds_file.exists()

    recent = store.get_recent_flights(limit=limit)
    callsigns = [r["normalized"] for r in recent]
    feeds_map = {r["normalized"]: (r.get("feeds") or "").split(",") for r in recent}

    console.print("[bold]OpenSky Flight Enrichment[/bold]")
    console.print(f"  Callsigns to enrich: {len(callsigns)}")
    console.print(f"  Enrichment DB: {enrichment_db}")
    console.print(f"  Mode: {'historical (' + airport + ')' if historical else 'live (bounding box)'}")
    if has_creds:
        console.print(f"  [green]✓ Credentials loaded from {creds_file}[/green]")
    else:
        console.print(f"  [yellow]⚠ No credentials file found at {creds_file}[/yellow]")
        console.print("    Download credentials.json from your OpenSky account page.")
    console.print()

    if historical:
        if not has_creds:
            console.print("[red]Error: historical mode requires OpenSky credentials.[/red]")
            sys.exit(1)
        _enrich_historical(store, service, callsigns, feeds_map, airport)
    else:
        _enrich_live(service, callsigns, feeds_map)


def _enrich_live(service, callsigns, feeds_map):
    """Enrich callsigns using live bounding-box state vectors."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Enriching...", total=len(callsigns))
        enriched = 0
        not_found = 0

        for cs in callsigns:
            progress.update(task, description=f"Enriching {cs}...")
            result = service.enrich(cs, feeds_heard=feeds_map.get(cs, []))
            if result and result.icao24:
                enriched += 1
                route = ""
                if result.origin or result.destination:
                    route = f" {result.origin or '?'} → {result.destination or '?'}"
                progress.console.print(
                    f"  [green]✓[/green] {cs}: {result.icao24}{route}"
                )
            else:
                not_found += 1
            progress.advance(task)

    console.print()
    console.print("[green]✓ Enrichment complete[/green]")
    console.print(f"  Matched in airspace: {enriched}")
    if not_found:
        console.print(f"  Not currently visible: {not_found}")


def _enrich_historical(store, service, callsigns, feeds_map, airport):
    """Enrich callsigns using historical airport arrival/departure data."""
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT MIN(td.start_time_utc) as earliest, MAX(td.end_time_utc) as latest
           FROM entity_mentions em
           JOIN transcript_docs td ON em.doc_id = td.doc_id
           WHERE em.entity_type = 'callsign'"""
    ).fetchone()
    conn.close()

    if not row or not row["earliest"] or not row["latest"]:
        console.print("[red]Error: no timestamped callsign data found.[/red]")
        return

    from datetime import datetime, timezone
    earliest = datetime.fromisoformat(row["earliest"]).astimezone(timezone.utc)
    latest = datetime.fromisoformat(row["latest"]).astimezone(timezone.utc)
    begin = int(earliest.timestamp()) - 3600
    end = int(latest.timestamp()) + 3600

    total_days = max(1, (end - begin) // 86400)
    console.print(f"  Recording window: {earliest:%Y-%m-%d %H:%M} → {latest:%Y-%m-%d %H:%M} UTC")
    console.print(f"  Fetching {airport} arrivals & departures ({total_days} days)...")
    console.print()

    airport_flights = service.client.get_airport_flights(airport, begin, end)
    console.print(f"  [green]✓[/green] Found {len(airport_flights)} unique flights at {airport}")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Matching callsigns...", total=len(callsigns))

        def on_progress(cs, flight):
            if flight:
                origin = flight.get("estDepartureAirport") or "?"
                dest = flight.get("estArrivalAirport") or "?"
                icao24 = flight.get("icao24", "")
                progress.console.print(
                    f"  [green]✓[/green] {cs}: {icao24} {origin} → {dest}"
                )
            progress.advance(task)

        stats = service.historical_enrich(
            callsigns=callsigns,
            feeds_map=feeds_map,
            airport_flights=airport_flights,
            on_progress=on_progress,
        )

    console.print()
    console.print("[green]✓ Historical enrichment complete[/green]")
    console.print(f"  Flights from {airport} API: {stats['api_flights']}")
    console.print(f"  Callsigns matched: {stats['matched']}")
    if stats["not_found"]:
        console.print(f"  Not found in {airport} data: {stats['not_found']}")


@cli.command("flight-track")
@click.argument("callsign")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.pass_context
def flight_track_cmd(ctx: click.Context, callsign: str, config: Optional[Path]) -> None:
    """Show a flight's cross-feed journey.

    CALLSIGN is the normalized callsign (e.g., DAL1234, N123AB).
    """
    cfg = load_config(config) if config else ctx.obj['config']

    if cfg.rag and cfg.rag.vector_store:
        db_path = Path(cfg.rag.vector_store.sqlite_metadata_path)
    else:
        db_path = cfg.output_dir / "rag_metadata.db"

    if not db_path.exists():
        console.print("[red]Error: metadata database not found.[/red]")
        sys.exit(1)

    from .ingest import MetadataStore
    from .flight_tracker import FlightTracker

    store = MetadataStore(db_path)
    store.ensure_schema()
    tracker = FlightTracker(metadata_store=store)
    track = tracker.track_flight(callsign.upper())

    if not track or not track.legs:
        console.print(f"[yellow]No data found for callsign {callsign.upper()}[/yellow]")
        return

    console.print(f"[bold]Flight Track: {track.callsign}[/bold]")
    console.print(f"  Total duration: {int(track.total_duration.total_seconds() / 60)} min")
    console.print(f"  Frequencies: {len(track.legs)}")
    console.print()

    table = Table(title="Frequency Timeline")
    table.add_column("#", style="dim")
    table.add_column("Feed", style="cyan")
    table.add_column("Frequency", style="yellow")
    table.add_column("First Heard", style="green")
    table.add_column("Last Heard", style="green")
    table.add_column("Segments", style="dim")
    table.add_column("Handoff", style="yellow")

    for i, leg in enumerate(track.legs, 1):
        table.add_row(
            str(i),
            leg.feed_id,
            leg.frequency or "—",
            leg.first_heard.strftime("%H:%M:%S"),
            leg.last_heard.strftime("%H:%M:%S"),
            str(len(leg.segments)),
            f"→ {leg.handoff_to}" if leg.handoff_to else "—",
        )

    console.print(table)


@cli.command("position-profile")
@click.argument("feed_id")
@click.option('--config', '-c', type=click.Path(exists=True, path_type=Path),
              help='Path to configuration file')
@click.option('--start-time', help='Start time filter (ISO-8601)')
@click.option('--end-time', help='End time filter (ISO-8601)')
@click.pass_context
def position_profile_cmd(
    ctx: click.Context,
    feed_id: str,
    config: Optional[Path],
    start_time: Optional[str],
    end_time: Optional[str],
) -> None:
    """Show ATC position analytics for a feed.

    FEED_ID is the feed identifier (e.g., kdca1_twr).
    """
    cfg = load_config(config) if config else ctx.obj['config']

    if cfg.rag and cfg.rag.vector_store:
        db_path = Path(cfg.rag.vector_store.sqlite_metadata_path)
    else:
        db_path = cfg.output_dir / "rag_metadata.db"

    if not db_path.exists():
        console.print("[red]Error: metadata database not found.[/red]")
        sys.exit(1)

    from .controller_profile import ControllerProfiler

    profiler = ControllerProfiler(db_path)
    profile = profiler.profile_feed(feed_id, start_time=start_time, end_time=end_time)

    console.print(f"[bold]Position Profile: {feed_id}[/bold]")
    if profile.time_window_start and profile.time_window_end:
        console.print(f"  Time range: {profile.time_window_start.strftime('%Y-%m-%d %H:%M')} – {profile.time_window_end.strftime('%Y-%m-%d %H:%M')} UTC")
    console.print()

    table = Table(title="Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Total segments", str(profile.total_segments))
    table.add_row("ATC segments", str(profile.atc_segments))
    table.add_row("Pilot segments", str(profile.pilot_segments))
    table.add_row("Unique callsigns", str(profile.unique_callsigns))
    table.add_row("Avg segment duration", f"{profile.avg_segment_duration:.1f}s")
    table.add_row("Total talk time", f"{profile.total_talk_time:.0f}s ({profile.total_talk_time / 60:.1f} min)")
    console.print(table)

    if profile.phrases:
        console.print()
        phrase_table = Table(title="Top Phraseology")
        phrase_table.add_column("Phrase", style="green")
        phrase_table.add_column("Count", style="white")
        for phrase, count in sorted(profile.phrases.items(), key=lambda x: -x[1])[:15]:
            phrase_table.add_row(phrase, str(count))
        console.print(phrase_table)

    if profile.busiest_hours:
        console.print()
        hour_table = Table(title="Busiest Hours (UTC)")
        hour_table.add_column("Hour", style="yellow")
        hour_table.add_column("Segments", style="white")
        for hour, count in sorted(profile.busiest_hours, key=lambda x: -x[1])[:10]:
            hour_table.add_row(f"{hour:02d}:00", str(count))
        console.print(hour_table)

    if profile.callsign_list:
        console.print()
        console.print(f"[dim]Callsigns seen: {', '.join(profile.callsign_list[:30])}"
                       + (f" ... and {len(profile.callsign_list) - 30} more" if len(profile.callsign_list) > 30 else "")
                       + "[/dim]")


@cli.group()
def variants() -> None:
    """Manage transcript variants (multiple ASR outputs and edits)."""
    pass


@variants.command("list")
@click.option("--audio-file", help="Filter by audio filename (e.g. kdca1_twr_2026-02-19_0003Z.mp3)")
@click.option("--feed", help="Filter by feed ID (e.g. kdca1_twr)")
@click.option("--model", help="Filter by ASR model name")
@click.option("--limit", default=100, type=int, help="Max results to show")
@click.pass_context
def variants_list(
    ctx: click.Context,
    audio_file: Optional[str],
    feed: Optional[str],
    model: Optional[str],
    limit: int,
) -> None:
    """List transcript variants with optional filters."""
    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    from .variant_store import TranscriptVariantStore
    items = vs.list_variants(audio_file=audio_file, feed_id=feed, asr_model=model, limit=limit)

    if not items:
        console.print("[yellow]No variants found.[/yellow]")
        return

    table = Table(title=f"Transcript Variants ({len(items)} results)")
    table.add_column("ID", style="dim", max_width=10)
    table.add_column("Audio File", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Preprocess", style="yellow")
    table.add_column("Type", style="dim")
    table.add_column("Words", justify="right")
    table.add_column("Segs", justify="right")
    table.add_column("Active", justify="center")
    table.add_column("Created", style="dim")

    for v in items:
        created_short = v.created_at[:19] if v.created_at else ""
        table.add_row(
            v.variant_id[:10],
            v.audio_file,
            v.asr_model,
            v.preprocess,
            v.variant_type,
            str(v.word_count),
            str(v.segment_count),
            "[green]yes[/green]" if v.is_active else "no",
            created_short,
        )

    console.print(table)


@variants.command("show")
@click.argument("variant_id")
@click.option("--full", is_flag=True, help="Show full transcript JSON")
@click.pass_context
def variants_show(ctx: click.Context, variant_id: str, full: bool) -> None:
    """Display a variant's transcript."""
    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    variant = vs.get_variant(variant_id)
    if variant is None:
        # Try prefix match
        matches = [v for v in vs.list_variants(limit=500) if v.variant_id.startswith(variant_id)]
        if len(matches) == 1:
            variant = matches[0]
        elif len(matches) > 1:
            console.print(f"[yellow]Ambiguous ID prefix '{variant_id}'. Matches:[/yellow]")
            for m in matches:
                console.print(f"  {m.variant_id[:10]}  {m.audio_file}  {m.asr_model}/{m.preprocess}")
            return
        else:
            console.print(f"[red]Variant not found: {variant_id}[/red]")
            return

    console.print(f"[bold]Variant: {variant.variant_id}[/bold]")
    console.print(f"  Audio file:  {variant.audio_file}")
    console.print(f"  Feed:        {variant.feed_id}")
    console.print(f"  ASR model:   {variant.asr_model}")
    console.print(f"  Preprocess:  {variant.preprocess}")
    console.print(f"  Type:        {variant.variant_type}")
    console.print(f"  Active:      {'yes' if variant.is_active else 'no'}")
    console.print(f"  Words:       {variant.word_count}")
    console.print(f"  Segments:    {variant.segment_count}")
    console.print(f"  Created:     {variant.created_at}")
    console.print(f"  Created by:  {variant.created_by}")
    if variant.parent_variant_id:
        console.print(f"  Parent:      {variant.parent_variant_id}")
    if variant.notes:
        console.print(f"  Notes:       {variant.notes}")
    console.print()

    if full:
        import json
        console.print_json(json.dumps(variant.transcript, indent=2, ensure_ascii=False))
    else:
        text = variant.transcript.get("text", "")
        console.print("[bold]Transcript text:[/bold]")
        console.print(text if text else "[dim](empty)[/dim]")


@variants.command("compare")
@click.argument("variant_a")
@click.argument("variant_b")
@click.pass_context
def variants_compare(ctx: click.Context, variant_a: str, variant_b: str) -> None:
    """Compare two transcript variants side by side.

    VARIANT_A and VARIANT_B are variant IDs (or unique prefixes).
    """
    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    def _resolve(vid: str):
        v = vs.get_variant(vid)
        if v:
            return v.variant_id
        matches = [x for x in vs.list_variants(limit=500) if x.variant_id.startswith(vid)]
        if len(matches) == 1:
            return matches[0].variant_id
        return None

    id_a = _resolve(variant_a)
    id_b = _resolve(variant_b)
    if not id_a:
        console.print(f"[red]Cannot resolve variant: {variant_a}[/red]")
        return
    if not id_b:
        console.print(f"[red]Cannot resolve variant: {variant_b}[/red]")
        return

    diff = vs.compare_variants(id_a, id_b)
    if diff is None:
        console.print("[red]Error computing diff.[/red]")
        return

    va = vs.get_variant(id_a)
    vb = vs.get_variant(id_b)

    console.print("[bold]Variant Comparison[/bold]")
    console.print(f"  A: {id_a[:10]}  ({va.asr_model}/{va.preprocess})  {diff.word_count_a} words, {diff.segment_count_a} segments")
    console.print(f"  B: {id_b[:10]}  ({vb.asr_model}/{vb.preprocess})  {diff.word_count_b} words, {diff.segment_count_b} segments")
    console.print()

    if diff.unified_diff:
        console.print("[bold]Text diff:[/bold]")
        for line in diff.unified_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                console.print(f"[green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                console.print(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                console.print(f"[cyan]{line}[/cyan]")
            else:
                console.print(line)
    else:
        console.print("[green]Texts are identical.[/green]")

    if diff.segment_diffs:
        console.print()
        console.print(f"[bold]Segment-level differences ({len(diff.segment_diffs)}):[/bold]")
        seg_table = Table()
        seg_table.add_column("Time", style="dim")
        seg_table.add_column("A", style="red")
        seg_table.add_column("B", style="green")
        for sd in diff.segment_diffs[:30]:
            seg_table.add_row(sd["time_range"], sd["text_a"][:80], sd["text_b"][:80])
        console.print(seg_table)
        if len(diff.segment_diffs) > 30:
            console.print(f"[dim]... and {len(diff.segment_diffs) - 30} more[/dim]")


@variants.command("activate")
@click.argument("variant_id")
@click.pass_context
def variants_activate(ctx: click.Context, variant_id: str) -> None:
    """Promote a variant to active (writes its content to the .json on disk)."""
    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    def _resolve(vid: str):
        v = vs.get_variant(vid)
        if v:
            return v.variant_id
        matches = [x for x in vs.list_variants(limit=500) if x.variant_id.startswith(vid)]
        if len(matches) == 1:
            return matches[0].variant_id
        return None

    resolved = _resolve(variant_id)
    if not resolved:
        console.print(f"[red]Variant not found: {variant_id}[/red]")
        sys.exit(1)

    ok = vs.activate_variant(resolved)
    if ok:
        v = vs.get_variant(resolved)
        console.print(f"[green]Activated variant {resolved[:10]} for {v.audio_file}[/green]")
    else:
        console.print(f"[red]Failed to activate variant {variant_id}[/red]")
        sys.exit(1)


@variants.command("import")
@click.argument("json_file", type=click.Path(exists=True, path_type=Path))
@click.option("--parent", help="Parent variant ID (the variant this edit is based on)")
@click.option("--notes", help="Optional notes about this edit")
@click.option("--created-by", default="user", help="Who created this edit")
@click.pass_context
def variants_import(
    ctx: click.Context,
    json_file: Path,
    parent: Optional[str],
    notes: Optional[str],
    created_by: str,
) -> None:
    """Import an edited transcript JSON as a new variant.

    JSON_FILE is the path to the edited transcript JSON.
    """
    import json as _json

    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    try:
        with open(json_file, "r", encoding="utf-8") as f:
            transcript_data = _json.load(f)
    except Exception as exc:
        console.print(f"[red]Failed to read JSON: {exc}[/red]")
        sys.exit(1)

    audio_file = transcript_data.get("audio_file")
    if not audio_file:
        console.print("[red]JSON missing 'audio_file' field[/red]")
        sys.exit(1)

    if parent:
        def _resolve(vid: str):
            v = vs.get_variant(vid)
            if v:
                return v.variant_id
            matches = [x for x in vs.list_variants(limit=500) if x.variant_id.startswith(vid)]
            if len(matches) == 1:
                return matches[0].variant_id
            return None

        resolved_parent = _resolve(parent)
        if not resolved_parent:
            console.print(f"[red]Parent variant not found: {parent}[/red]")
            sys.exit(1)

        vid = vs.save_edit(
            audio_file=audio_file,
            transcript_data=transcript_data,
            parent_variant_id=resolved_parent,
            notes=notes,
            created_by=created_by,
        )
    else:
        try:
            rel_path = str(json_file.parent.relative_to(cfg.output_dir))
        except ValueError:
            rel_path = str(json_file.parent)

        vid = vs.save_variant(
            audio_file=audio_file,
            audio_path=rel_path,
            asr_model="manual",
            preprocess="n/a",
            transcript_data=transcript_data,
            variant_type="edit",
            activate=False,
            created_by=created_by,
            notes=notes,
        )

    console.print(f"[green]Imported variant {vid[:10]} for {audio_file}[/green]")


@variants.command("backfill")
@click.option("--dir", "recordings_dir", type=click.Path(exists=True, path_type=Path),
              help="Recordings directory to scan (default: from config)")
@click.option("--model", default="whisper-large-v3",
              help="ASR model to assign to existing transcripts")
@click.option("--preprocess", default="unknown",
              help="Preprocessing method to assign to existing transcripts")
@click.pass_context
def variants_backfill(
    ctx: click.Context,
    recordings_dir: Optional[Path],
    model: str,
    preprocess: str,
) -> None:
    """Scan existing transcript JSON files and register them as variants.

    Idempotent -- re-running skips already-registered files.
    """
    cfg = ctx.obj["config"]
    vs = _build_variant_store(cfg)
    if vs is None:
        console.print("[red]Error: variant store not available[/red]")
        sys.exit(1)

    scan_dir = recordings_dir or cfg.output_dir

    console.print(f"[bold]Backfilling variants from {scan_dir}[/bold]")
    console.print(f"  Default model: {model}")
    console.print(f"  Default preprocess: {preprocess}")
    console.print()

    with console.status("[bold blue]Scanning..."):
        count = vs.backfill(scan_dir, asr_model=model, preprocess=preprocess)

    console.print(f"[green]Backfill complete: {count} new variants registered[/green]")


if __name__ == '__main__':
    cli()

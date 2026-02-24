from click.testing import CliRunner

from atc_recorder.cli import cli


def test_cli_help_smoke():
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "ATC Recorder" in result.output


def test_cli_feeds_help_smoke():
    runner = CliRunner()

    result = runner.invoke(cli, ["feeds", "--help"])

    assert result.exit_code == 0
    assert "Manage and discover ATC feeds" in result.output

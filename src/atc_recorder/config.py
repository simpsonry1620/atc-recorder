"""Configuration management for ATC Recorder."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class RecordingConfig:
    """Configuration for recording behavior."""
    
    enabled: bool = True
    reconnect_delay: int = 30  # seconds
    max_retries: int = 5
    segment_duration: int = 1800  # 30 minutes in seconds


@dataclass
class Config:
    """Main configuration for ATC Recorder."""
    
    output_dir: Path = field(default_factory=lambda: Path("./recordings"))
    segment_duration: int = 1800  # 30 minutes in seconds
    feeds: list[str] = field(default_factory=list)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    request_delay: float = 1.0  # delay between requests in seconds
    user_agent: str = "ATC-Recorder/0.1.0"
    
    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        """Load configuration from a YAML file.
        
        Args:
            path: Path to the YAML configuration file
            
        Returns:
            Config object
            
        Raises:
            FileNotFoundError: If the config file doesn't exist
            yaml.YAMLError: If the YAML is invalid
        """
        with open(path, 'r') as f:
            data = yaml.safe_load(f) or {}
        
        return cls.from_dict(data)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Create a Config from a dictionary.
        
        Args:
            data: Configuration dictionary
            
        Returns:
            Config object
        """
        recording_data = data.get('recording', {})
        recording_config = RecordingConfig(
            enabled=recording_data.get('enabled', True),
            reconnect_delay=recording_data.get('reconnect_delay', 30),
            max_retries=recording_data.get('max_retries', 5),
            segment_duration=recording_data.get('segment_duration', 1800),
        )
        
        output_dir = data.get('output_dir', './recordings')
        if isinstance(output_dir, str):
            output_dir = Path(output_dir)
        
        return cls(
            output_dir=output_dir,
            segment_duration=data.get('segment_duration', 1800),
            feeds=data.get('feeds', []),
            recording=recording_config,
            request_delay=data.get('request_delay', 1.0),
            user_agent=data.get('user_agent', 'ATC-Recorder/0.1.0'),
        )
    
    def to_dict(self) -> dict:
        """Convert the configuration to a dictionary.
        
        Returns:
            Configuration dictionary
        """
        return {
            'output_dir': str(self.output_dir),
            'segment_duration': self.segment_duration,
            'feeds': self.feeds,
            'recording': {
                'enabled': self.recording.enabled,
                'reconnect_delay': self.recording.reconnect_delay,
                'max_retries': self.recording.max_retries,
                'segment_duration': self.recording.segment_duration,
            },
            'request_delay': self.request_delay,
            'user_agent': self.user_agent,
        }
    
    def save(self, path: Path) -> None:
        """Save configuration to a YAML file.
        
        Args:
            path: Path to save the configuration
        """
        with open(path, 'w') as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)


def load_config(path: Optional[Path] = None) -> Config:
    """Load configuration from file or return defaults.
    
    Args:
        path: Optional path to config file. If None, looks for config.yaml
              in the current directory.
              
    Returns:
        Config object
    """
    if path is None:
        path = Path("config.yaml")
    
    if path.exists():
        return Config.from_yaml(path)
    
    return Config()


# Default DCA feeds for quick access
DCA_FEEDS = [
    "kdca1_gnd",        # Ground (121.700)
    "kdca2_twr",        # Tower 1 (119.100)
    "kdca1_twr",        # Tower 2 (119.100)
    "kdca1_heli",       # Tower Helicopters (134.350)
    "kdca1_dep",        # Potomac Departure (118.950)
    "kdca1_app_final",  # Approach DCA Final (124.700)
    "kdca1_app_ensue",  # Approach ENSUE Sector (124.200)
    "kdca1_app_ojaay",  # Approach OJAAY Sector (119.850)
    "kmrb1_app_luray",  # Approach LURAY (118.675)
    "kdca1_dep_121050", # App/Dep FLUKY (121.050)
    "kdca1_dep_e",      # App/Dep KRANT (125.650)
    "kdca1_sfra_s",     # SFRA South (125.125)
    "kdca",             # Tower/Approach Combined
]

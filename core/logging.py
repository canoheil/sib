"""Structured logging and event definitions for sib."""

import logging
import sys
from typing import Any, Dict, Optional


class Events:
    """Standard structured audit events for sib bot."""
    USER_REGISTERED = "USER_REGISTERED"
    GROUP_REGISTERED = "GROUP_REGISTERED"
    MEMBERSHIP_VERIFIED = "MEMBERSHIP_VERIFIED"
    MEMBERSHIP_REJECTED = "MEMBERSHIP_REJECTED"
    
    PARTY_CREATED = "PARTY_CREATED"
    PARTY_JOINED = "PARTY_JOINED"
    PARTY_LEFT = "PARTY_LEFT"
    
    MATCHMAKING_STARTED = "MATCHMAKING_STARTED"
    MATCHMAKING_CANCELLED = "MATCHMAKING_CANCELLED"
    MATCHMAKING_TIMEOUT = "MATCHMAKING_TIMEOUT"
    
    MATCH_CREATED = "MATCH_CREATED"
    MATCH_FOUND = "MATCH_FOUND"
    MATCH_ACCEPTED = "MATCH_ACCEPTED"
    MATCH_REJECTED = "MATCH_REJECTED"
    MATCH_STARTED = "MATCH_STARTED"
    MOVE_PLAYED = "MOVE_PLAYED"
    MATCH_FINISHED = "MATCH_FINISHED"
    SEEDLING_AWARDED = "SEEDLING_AWARDED"
    APPLE_TRANSACTION = "APPLE_TRANSACTION"
    GARDEN_EVENT = "GARDEN_EVENT"
    MATCH_CANCELLED = "MATCH_CANCELLED"
    
    RATING_UPDATED = "RATING_UPDATED"
    AD_DISPLAYED = "AD_DISPLAYED"
    AD_CLICKED = "AD_CLICKED"
    
    ADMIN_COMMAND_EXECUTED = "ADMIN_COMMAND_EXECUTED"
    SYSTEM_RESTART_RECOVERY = "SYSTEM_RESTART_RECOVERY"


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Set up structured logging."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    
    root_logger = logging.getLogger("sib")
    root_logger.setLevel(numeric_level)
    
    # Avoid duplicate handlers if called multiple times
    if not root_logger.handlers:
        root_logger.addHandler(handler)
        
    return root_logger


logger = setup_logging()


def log_event(event_name: str, details: Optional[Dict[str, Any]] = None, level: int = logging.INFO) -> None:
    """Log a structured domain event."""
    extra_str = f" | {details}" if details else ""
    logger.log(level, f"[EVENT:{event_name}]{extra_str}")

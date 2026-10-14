"""Compatibility names for static, AI-painted background assets."""
from .templates import load_background


def background(enemy_type: str):
    return load_background(enemy_type)


def soldier(): return load_background("soldier")
def shadow(): return load_background("shadow")
def commander(): return load_background("commander")
def brute(): return load_background("brute")
def king(): return load_background("king")

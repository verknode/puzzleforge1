"""PuzzleForge public API."""

from .model import Puzzle
from .registry import get_puzzle, puzzles

__all__ = ["Puzzle", "get_puzzle", "puzzles"]
__version__ = "0.13.0"

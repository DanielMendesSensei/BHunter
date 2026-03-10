"""
Codebase Analyzer Tool for BHunter.

Custom Agno toolkit that allows the agent to read and analyze
source files from the local repository. This gives the agent
full codebase context to understand bugs and generate fixes.

Features:
  - Read file contents (with line range support)
  - Search for patterns in the codebase
  - List directory contents
  - Check file existence
  - Get file metadata (size, language)

Security:
  - All paths are resolved relative to the project root
  - Path traversal is blocked
  - Binary files are skipped
  - File blocklist is enforced

All credentials are read from BHunterConfig -- no global settings.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from agno.tools import Toolkit

from bhunter.config import BHunterConfig

logger = logging.getLogger("bhunter.tools.codebase")

# Max file size to read (500KB)
MAX_FILE_SIZE = 500_000

# Extensions we can read
TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".md", ".txt", ".html", ".css", ".scss",
    ".sql", ".sh", ".bash", ".env.example",
}


class CodebaseToolkit(Toolkit):
    """
    Toolkit for reading and analyzing the local codebase.

    Gives the BHunter agent the ability to understand code context
    around bugs identified by Sentry stacktraces.
    """

    def __init__(
        self,
        project_root: str | None = None,
        config: BHunterConfig | None = None,
    ):
        super().__init__(name="codebase_analyzer")

        # Resolve project root: explicit > config > git detect > cwd
        if project_root:
            self.project_root = Path(project_root).resolve()
        elif config and config.project_root:
            self.project_root = Path(config.project_root).resolve()
        else:
            self.project_root = self._detect_project_root()

        self.config = config or BHunterConfig()

        # Register tools
        self.register(self.read_file)
        self.register(self.search_code)
        self.register(self.list_directory)
        self.register(self.file_exists)
        self.register(self.get_file_info)

    @staticmethod
    def _detect_project_root() -> Path:
        """Detect project root via git or fallback to cwd."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip()).resolve()
        except Exception:
            pass
        return Path.cwd().resolve()

    def _safe_path(self, filepath: str) -> Path | None:
        """
        Resolve a filepath safely, preventing path traversal.
        Returns None if the path is outside project root or blocked.
        """
        try:
            # Resolve relative to project root
            resolved = (self.project_root / filepath).resolve()

            # Block path traversal
            if not str(resolved).startswith(str(self.project_root)):
                logger.warning("Path traversal blocked: %s", filepath)
                return None

            # Check blocklist
            if self.config.is_file_blocked(filepath):
                logger.info("Blocked file access: %s", filepath)
                return None

            return resolved
        except Exception:
            return None

    def read_file(
        self,
        filepath: str,
        start_line: int = 1,
        end_line: int = 0,
    ) -> str:
        """
        Read the contents of a source file from the codebase.

        Args:
            filepath: Relative path from project root (e.g. "apps/server/src/main.py")
            start_line: First line to read (1-based, default: 1)
            end_line: Last line to read (0 = until end of file)

        Returns:
            File contents as string, or error message if file cannot be read.
        """
        safe = self._safe_path(filepath)
        if safe is None:
            return f"Error: Cannot access '{filepath}' (blocked or invalid path)"

        if not safe.exists():
            return f"Error: File not found: '{filepath}'"

        if not safe.is_file():
            return f"Error: '{filepath}' is not a file"

        # Check extension
        if safe.suffix not in TEXT_EXTENSIONS:
            return f"Error: Cannot read binary/unsupported file type: {safe.suffix}"

        # Check size
        size = safe.stat().st_size
        if size > MAX_FILE_SIZE:
            return f"Error: File too large ({size} bytes, max {MAX_FILE_SIZE})"

        try:
            content = safe.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)

            # Apply line range
            start_idx = max(0, start_line - 1)
            if end_line > 0:
                end_idx = min(len(lines), end_line)
            else:
                end_idx = len(lines)

            selected = lines[start_idx:end_idx]

            # Add line numbers for context
            numbered = []
            for i, line in enumerate(selected, start=start_idx + 1):
                numbered.append(f"{i:4d} | {line.rstrip()}")

            return (
                f"File: {filepath} (lines {start_idx + 1}-{end_idx} of {len(lines)})\n"
                + "\n".join(numbered)
            )
        except UnicodeDecodeError:
            return f"Error: Cannot decode '{filepath}' as UTF-8"
        except Exception as exc:
            return f"Error reading '{filepath}': {exc}"

    def search_code(
        self,
        pattern: str,
        file_glob: str = "",
        max_results: int = 20,
    ) -> str:
        """
        Search for a pattern in the codebase using grep.

        Args:
            pattern: Text or regex pattern to search for
            file_glob: Optional glob filter (e.g. "*.py", "*.ts")
            max_results: Maximum number of results to return

        Returns:
            Matching lines with file paths and line numbers.
        """
        cmd = [
            "grep", "-rn", "--include", file_glob or "*",
            "-m", str(max_results),
            pattern,
            str(self.project_root),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.project_root),
            )

            if result.returncode == 1:
                return f"No matches found for pattern: '{pattern}'"

            if not result.stdout.strip():
                return f"No matches found for pattern: '{pattern}'"

            # Make paths relative
            output = result.stdout.replace(str(self.project_root) + "/", "")

            lines = output.strip().splitlines()
            return (
                f"Found {len(lines)} match(es) for '{pattern}':\n"
                + "\n".join(lines[:max_results])
            )
        except subprocess.TimeoutExpired:
            return "Error: Search timed out after 30 seconds"
        except Exception as exc:
            return f"Error searching codebase: {exc}"

    def list_directory(self, dirpath: str = "") -> str:
        """
        List the contents of a directory in the project.

        Args:
            dirpath: Relative path from project root (empty = root)

        Returns:
            Directory listing with file types.
        """
        safe = self._safe_path(dirpath) if dirpath else self.project_root

        if safe is None:
            return f"Error: Cannot access directory '{dirpath}'"

        if not safe.is_dir():
            return f"Error: '{dirpath}' is not a directory"

        try:
            entries = sorted(safe.iterdir())
            items = []
            for entry in entries:
                if entry.name.startswith(".") or entry.name == "__pycache__":
                    continue
                if entry.name == "node_modules":
                    continue
                kind = "DIR " if entry.is_dir() else "FILE"
                rel = str(entry.relative_to(self.project_root))
                items.append(f"  {kind}  {rel}")

            return (
                f"Directory: {dirpath or '.'} ({len(items)} items)\n"
                + "\n".join(items)
            )
        except Exception as exc:
            return f"Error listing directory: {exc}"

    def file_exists(self, filepath: str) -> str:
        """
        Check if a file exists in the project.

        Args:
            filepath: Relative path from project root

        Returns:
            "true" or "false"
        """
        safe = self._safe_path(filepath)
        if safe is None:
            return "false"
        return "true" if safe.exists() else "false"

    def get_file_info(self, filepath: str) -> str:
        """
        Get metadata about a file (size, extension, line count).

        Args:
            filepath: Relative path from project root

        Returns:
            File info as formatted string.
        """
        safe = self._safe_path(filepath)
        if safe is None:
            return f"Error: Cannot access '{filepath}'"

        if not safe.exists():
            return f"Error: File not found: '{filepath}'"

        stat = safe.stat()
        ext = safe.suffix

        # Count lines for text files
        lines = "N/A"
        if ext in TEXT_EXTENSIONS and stat.st_size < MAX_FILE_SIZE:
            try:
                lines = str(len(safe.read_text().splitlines()))
            except Exception:
                pass

        return (
            f"File: {filepath}\n"
            f"  Size: {stat.st_size:,} bytes\n"
            f"  Extension: {ext}\n"
            f"  Lines: {lines}\n"
        )

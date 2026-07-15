"""MCP-owned SQLite and managed module artifact boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagasmith_core import Database
from sagasmith_core.database import sqlite_database_url

from .config import McpConfig


class SagaSmithStorage:
    def __init__(self, config: McpConfig) -> None:
        self.config = config
        config.prepare()
        self.database = Database(config.database_url or sqlite_database_url(config.database_path))

    def migrate(self) -> None:
        self.database.upgrade_schema()

    def status(self) -> dict[str, Any]:
        return {
            "home": str(self.config.home),
            "database": {
                "url": self.database.url,
                "path": str(self.config.database_path),
                "exists": self.config.database_path.exists(),
            },
            "modules_dir": str(self.config.modules_dir),
        }

    def write_module(self, name: str, content: str) -> Path:
        if not name.strip():
            raise ValueError("module name must not be empty")
        if len(content.encode("utf-8")) > 20 * 1024 * 1024:
            raise ValueError("module artifact exceeds the 20 MiB safety limit")
        filename = name if name.casefold().endswith(".md") else f"{name}.md"
        target = (self.config.modules_dir / filename).resolve()
        if target.parent != self.config.modules_dir.resolve():
            raise ValueError("module name must not contain a path")
        target.write_text(content, encoding="utf-8")
        return target

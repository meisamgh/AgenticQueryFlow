"""Opt-in connection definitions for allowlisted remote database adapters.

Secrets remain environment references, not public configuration or browser payloads.
The application registers these definitions lazily without opening connections.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from semantic_text2sql.database import DatabaseError


class ExternalConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    db_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    engine: Literal["bigquery", "mysql", "sqlserver", "snowflake"]
    url_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    schema_name: str = Field(min_length=1, max_length=200)
    allowed_tables: list[str] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=5, ge=1, le=60)
    maximum_bytes_billed: int | None = Field(default=None, ge=1)
    read_only_role_confirmed: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> ExternalConnection:
        if len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("Allowed tables must be unique.")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.allowed_tables):
            raise ValueError("Allowed tables must be simple identifiers.")
        if self.engine == "bigquery" and self.maximum_bytes_billed is None:
            raise ValueError("BigQuery requires an explicit byte-cost ceiling.")
        parts = self.schema_name.split(".")
        if len(parts) > 2 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", p) for p in parts):
            raise ValueError("Unsupported schema identifier.")
        if self.engine == "bigquery" and len(parts) != 2:
            raise ValueError("BigQuery schema must be project.dataset.")
        if self.engine in {"mysql", "sqlserver"} and len(parts) != 1:
            raise ValueError("Use one database/schema identifier for this adapter.")
        return self

    @property
    def dialect(self) -> str:
        return "tsql" if self.engine == "sqlserver" else self.engine

    def connection_url(self) -> str:
        value = os.environ.get(self.url_env, "")
        if not value:
            raise DatabaseError("CONNECTION_NOT_CONFIGURED", f"Set {self.url_env} locally.")
        prefix = {
            "mysql": "mysql+pymysql://",
            "sqlserver": "mssql+pyodbc://",
            "snowflake": "snowflake://",
            "bigquery": "bigquery://",
        }[self.engine]
        if not value.startswith(prefix):
            raise DatabaseError(
                "CONNECTION_SCHEME_MISMATCH",
                "Connection URL has the wrong driver.",
            )
        return value


def external_connections_from_environment() -> dict[str, ExternalConnection]:
    """Parse bounded configuration only. Never connect at import/startup."""
    raw = os.environ.get("TEXT2SQL_EXTERNAL_CONNECTIONS", "[]")
    if len(raw) > 64_000:
        raise ValueError("External connection configuration is too large.")
    entries = json.loads(raw)
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("Expected at most twenty external connections.")
    connections = [ExternalConnection.model_validate(item) for item in entries]
    if len({item.db_id for item in connections}) != len(connections):
        raise ValueError("Duplicate external database IDs.")
    return {item.db_id: item for item in connections}

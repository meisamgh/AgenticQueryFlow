"""SQLGlot dialect identifiers shared by the application and adapters."""

from typing import Literal

DatabaseDialect = Literal["sqlite", "postgres", "mysql", "tsql", "snowflake", "bigquery"]

"""Universal dataset schema + the adapter interface every data source implements.

Version 1 only ships the Kaggle adapter. A Hugging Face or UCI adapter would subclass
`DatasetAdapter`, return `DatasetRecord`s, and register itself in `dataset_service.ADAPTERS`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class DatasetFile:
    name: str
    size_bytes: int | None = None


@dataclass
class DatasetRecord:
    source: str                      # "kaggle", later "huggingface", "uci", ...
    ref: str                         # source-specific id, e.g. "owner/slug"
    title: str
    subtitle: str | None = None
    description: str | None = None
    owner: str | None = None
    creator: str | None = None
    url: str | None = None
    license: str | None = None
    size_bytes: int | None = None
    download_count: int | None = None
    vote_count: int | None = None
    view_count: int | None = None
    usability: float | None = None   # 0..1
    last_updated: str | None = None  # ISO 8601
    tags: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.ref}"


class DatasetAdapter(ABC):
    source: str = ""

    @abstractmethod
    def search(self, query: str = "", page: int = 1, sort_by: str = "hottest",
               file_type: str | None = None) -> list[DatasetRecord]:
        """Return one page of datasets matching `query`."""

    @abstractmethod
    def get_files(self, ref: str) -> list[DatasetFile]:
        """List the files inside a dataset."""

    @abstractmethod
    def get_description(self, ref: str) -> str | None:
        """Return the long-form description (often missing from list results)."""


class AdapterError(RuntimeError):
    """Raised for auth / network problems so the UI can show a friendly message."""

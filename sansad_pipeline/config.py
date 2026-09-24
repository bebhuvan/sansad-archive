from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StorageConfig:
    root: Path = Path("data")


@dataclass(frozen=True)
class LiteParseConfig:
    version: str = "2.14.7"
    language: str = "eng"
    dpi: int = 150
    full_page_image_dpi: int = 250
    workers: int = 4
    max_pages: int = 10000
    ocr_server_url: str = ""
    keep_headers_footers: bool = False
    preserve_small_text: bool = False


@dataclass(frozen=True)
class RoutingConfig:
    minimum_native_characters: int = 40
    minimum_alphanumeric_ratio: float = 0.35
    force_ocr_full_page_images: bool = True
    ocr_reasons: tuple[str, ...] = (
        "scanned", "no-text", "garbled", "vector-text", "annotation-text"
    )
    ignore_reasons: tuple[str, ...] = ("sparse-text", "embedded-images")


@dataclass(frozen=True)
class ValidationConfig:
    minimum_ocr_confidence: float = 0.70
    maximum_replacement_characters: int = 0
    flag_numeric_disagreement: bool = True
    flag_inconsistent_table_width: bool = True


@dataclass(frozen=True)
class OpenRouterConfig:
    enabled: bool = False
    endpoint: str = "https://openrouter.ai/api/v1/chat/completions"
    models_endpoint: str = "https://openrouter.ai/api/v1/models"
    model: str = ""
    models: tuple[str, ...] = ()
    max_tokens: int = 8192
    timeout_seconds: int = 300
    image_dpi: int = 150
    max_pages_per_command: int = 200
    max_cumulative_cost_usd: float = 10.0
    reasoning_effort: str = ""
    reasoning_exclude: bool = True
    store_reasoning: bool = False
    minimum_interval_seconds: float = 2.0
    max_retries: int = 6
    maximum_backoff_seconds: float = 60.0
    max_concurrency: int = 3


@dataclass(frozen=True)
class NvidiaConfig:
    enabled: bool = False
    endpoint: str = "https://integrate.api.nvidia.com/v1/chat/completions"
    model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    max_tokens: int = 8192
    reasoning_budget: int = 1024
    timeout_seconds: int = 240
    image_dpi: int = 150
    max_pages_per_command: int = 25
    minimum_interval_seconds: float = 3.0
    max_retries: int = 6
    maximum_backoff_seconds: float = 45.0
    max_concurrency: int = 5


@dataclass(frozen=True)
class Config:
    project_root: Path
    storage: StorageConfig = field(default_factory=StorageConfig)
    liteparse: LiteParseConfig = field(default_factory=LiteParseConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    nvidia: NvidiaConfig = field(default_factory=NvidiaConfig)

    @property
    def data_root(self) -> Path:
        root = self.storage.root
        return root if root.is_absolute() else self.project_root / root


def load_config(path: Path) -> Config:
    path = path.resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    storage = raw.get("storage", {})
    liteparse = raw.get("liteparse", {})
    routing = raw.get("routing", {})
    validation = raw.get("validation", {})
    openrouter = raw.get("openrouter", {})
    nvidia = raw.get("nvidia", {})
    return Config(
        project_root=path.parent,
        storage=StorageConfig(root=Path(storage.get("root", "data"))),
        liteparse=LiteParseConfig(**liteparse),
        routing=RoutingConfig(
            minimum_native_characters=routing.get("minimum_native_characters", 40),
            minimum_alphanumeric_ratio=routing.get("minimum_alphanumeric_ratio", 0.35),
            force_ocr_full_page_images=routing.get("force_ocr_full_page_images", True),
            ocr_reasons=tuple(routing.get("ocr_reasons", RoutingConfig().ocr_reasons)),
            ignore_reasons=tuple(routing.get("ignore_reasons", RoutingConfig().ignore_reasons)),
        ),
        validation=ValidationConfig(**validation),
        openrouter=OpenRouterConfig(
            **{
                **openrouter,
                "models": tuple(openrouter.get("models", ())),
            }
        ),
        nvidia=NvidiaConfig(**nvidia),
    )

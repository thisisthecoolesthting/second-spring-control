"""Configuration loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OpenAI
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model_draft: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL_DRAFT")
    openai_model_pillar: str = Field(default="gpt-4o", alias="OPENAI_MODEL_PILLAR")
    openai_model_safety: str = Field(default="gpt-4o", alias="OPENAI_MODEL_SAFETY")

    # Scout / Astro paths (Windows-friendly: use forward slashes in .env)
    scout_products_json: Path = Field(
        default=Path("../amazon-affiliate-scout/data/products.json"),
        alias="SCOUT_PRODUCTS_JSON",
    )
    astro_repo_dir: Path = Field(
        default=Path("../second-spring-club"),
        alias="ASTRO_REPO_DIR",
    )
    astro_content_dir: Path = Field(
        default=Path("../second-spring-club/src/content"),
        alias="ASTRO_CONTENT_DIR",
    )

    # Site / brand
    site_url: str = Field(default="https://secondspringclub.com", alias="SITE_URL")
    associate_tag: str = Field(default="secondspring-20", alias="AMAZON_ASSOCIATE_TAG")

    # Behavior
    max_parallel_drafts: int = Field(default=8, alias="MAX_PARALLEL_DRAFTS")
    auto_push: bool = Field(default=True, alias="AUTO_PUSH")
    control_db_path: Path = Field(default=Path("./control.db"), alias="CONTROL_DB_PATH")

    # Automation
    auto_advance_enabled: bool = Field(default=True, alias="AUTO_ADVANCE_ENABLED")
    auto_advance_interval_sec: int = Field(default=60, alias="AUTO_ADVANCE_INTERVAL_SEC")
    auto_scout_enabled: bool = Field(default=True, alias="AUTO_SCOUT_ENABLED")
    auto_scout_interval_hours: int = Field(default=6, alias="AUTO_SCOUT_INTERVAL_HOURS")
    auto_scout_max_items: int = Field(default=200, alias="AUTO_SCOUT_MAX_ITEMS")
    scout_project_dir: Path = Field(
        default=Path("../amazon-affiliate-scout"),
        alias="SCOUT_PROJECT_DIR",
    )
    # Auto-pipeline: draft + critique + meta + SEO on skeletons.
    auto_pipeline_enabled: bool = Field(default=True, alias="AUTO_PIPELINE_ENABLED")
    auto_pipeline_interval_min: int = Field(default=15, alias="AUTO_PIPELINE_INTERVAL_MIN")
    auto_pipeline_batch: int = Field(default=4, alias="AUTO_PIPELINE_BATCH")

    # Auto-import: when the scout cron updates products.json, pull fresh
    # ASINs into the controller DB without human action. Every 5 min checks
    # mtime; if changed, imports.
    auto_import_enabled: bool = Field(default=True, alias="AUTO_IMPORT_ENABLED")
    auto_import_interval_min: int = Field(default=5, alias="AUTO_IMPORT_INTERVAL_MIN")

    # Auto-cleanup: reset items stuck in transient stages (drafting, editing,
    # reviewing) for longer than the threshold. Runs every 15 min.
    auto_cleanup_enabled: bool = Field(default=True, alias="AUTO_CLEANUP_ENABLED")
    auto_cleanup_interval_min: int = Field(default=15, alias="AUTO_CLEANUP_INTERVAL_MIN")
    auto_cleanup_stuck_hours: int = Field(default=1, alias="AUTO_CLEANUP_STUCK_HOURS")

    # Auto-internal-linking: after new products land as `live`, go back
    # and add a "Related picks" block to each with 2-3 same-category
    # siblings. Boosts dwell time and distributes PageRank.
    auto_links_enabled: bool = Field(default=True, alias="AUTO_LINKS_ENABLED")
    auto_links_interval_min: int = Field(default=30, alias="AUTO_LINKS_INTERVAL_MIN")
    auto_links_per_item: int = Field(default=3, alias="AUTO_LINKS_PER_ITEM")

    # Unsplash integration — fetches relevant hero images for pillars and
    # articles that lack one. Free demo key at unsplash.com/developers
    # gives 50 requests/hour which is plenty for our volume.
    unsplash_access_key: str = Field(default="", alias="UNSPLASH_ACCESS_KEY")
    auto_images_enabled: bool = Field(default=True, alias="AUTO_IMAGES_ENABLED")
    auto_images_interval_min: int = Field(default=30, alias="AUTO_IMAGES_INTERVAL_MIN")

    # Drip publishing: keeps Google from flagging a burst pattern.
    auto_publish_enabled: bool = Field(default=False, alias="AUTO_PUBLISH_ENABLED")
    auto_publish_interval_min: int = Field(default=90, alias="AUTO_PUBLISH_INTERVAL_MIN")
    auto_publish_batch: int = Field(default=2, alias="AUTO_PUBLISH_BATCH")
    # Idea backlog auto-backfill
    auto_ideas_enabled: bool = Field(default=True, alias="AUTO_IDEAS_ENABLED")
    auto_ideas_min_backlog: int = Field(default=20, alias="AUTO_IDEAS_MIN_BACKLOG")
    auto_ideas_topics: str = Field(
        default=(
            "cooling solutions for menopause night sweats|"
            "supplements for perimenopause sleep|"
            "skincare for hormonal acne in your 40s|"
            "strength training gear for bone density after 45|"
            "books on perimenopause and midlife|"
            "cooling pajamas and loungewear|"
            "pelvic floor support products|"
            "hot flash management tools"
        ),
        alias="AUTO_IDEAS_TOPICS",
    )
    dashboard_refresh_sec: int = Field(default=30, alias="DASHBOARD_REFRESH_SEC")

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key) and self.openai_api_key.startswith("sk-")


settings = Settings()

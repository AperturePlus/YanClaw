from __future__ import annotations

import asyncio
from pathlib import Path

import click

from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import setup_logging
from runtime.skills import SkillManager


@click.group()
def cli() -> None:
    """Yanclaw command line interface."""


@cli.command()
@click.option("--universities", default="", help="Comma-separated university names to crawl.")
@click.option("--concurrency", default=None, type=int, help="Override max concurrency.")
@click.option("--log-dir", default=None, type=click.Path(path_type=Path), help="Log directory.")
@click.option("--skip-llm-check", is_flag=True, help="Skip the startup LLM connectivity check.")
def crawl(
    universities: str,
    concurrency: int | None,
    log_dir: Path | None,
    skip_llm_check: bool,
) -> None:
    """Start crawler agents."""

    overrides: dict[str, object] = {}
    if concurrency is not None:
        overrides["max_concurrency"] = concurrency
    if log_dir is not None:
        overrides["log_dir"] = log_dir
    settings = CrawlerSettings().model_copy(update=overrides) if overrides else CrawlerSettings()
    setup_logging(settings.log_dir)
    selected = [item.strip() for item in universities.split(",") if item.strip()] or None
    asyncio.run(_crawl_async(settings, selected, skip_llm_check=skip_llm_check))


async def _crawl_async(
    settings: CrawlerSettings,
    universities: list[str] | None,
    *,
    skip_llm_check: bool = False,
) -> None:
    if not skip_llm_check:
        await _check_llm(settings)
    dispatcher = CrawlDispatcher(settings=settings)
    summary = await dispatcher.run(universities)
    click.echo(f"success={summary.success} failed={summary.failed} skipped={summary.skipped}")


async def _check_llm(settings: CrawlerSettings) -> None:
    if not settings.openai_api_key:
        raise click.ClickException(
            "YANCLAW_OPENAI_API_KEY is empty. Set it in .env before running crawl."
        )

    client = LLMClient(
        settings.openai_base_url,
        settings.openai_api_key,
        settings.openai_model,
        max_rounds=1,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    try:
        await client.chat(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly: ok",
                }
            ],
            tools=None,
            tool_handlers={},
            max_tokens=1,
        )
    except Exception as error:
        raise click.ClickException(
            "LLM startup check failed. Check YANCLAW_OPENAI_BASE_URL, "
            f"YANCLAW_OPENAI_MODEL, and YANCLAW_OPENAI_API_KEY. Details: {error}"
        ) from error


@cli.command("llm-check")
def llm_check() -> None:
    """Check LLM connectivity and print the active endpoint/model."""

    settings = CrawlerSettings()
    click.echo(f"base_url={settings.openai_base_url}")
    click.echo(f"model={settings.openai_model}")
    click.echo(f"api_key_set={bool(settings.openai_api_key)}")
    click.echo(f"timeout_seconds={settings.llm_timeout_seconds}")
    asyncio.run(_check_llm(settings))
    click.echo("ok")


@cli.group()
def skills() -> None:
    """Manage crawler skills."""


@skills.command("list")
def list_skills() -> None:
    """List skills."""

    asyncio.run(_list_skills_async())


@skills.command("history")
@click.argument("name")
def skill_history(name: str) -> None:
    """Show skill version history."""

    asyncio.run(_history_async(name))


@skills.command("diff")
@click.argument("name")
@click.argument("v1", type=int)
@click.argument("v2", type=int)
def skill_diff(name: str, v1: int, v2: int) -> None:
    """Show unified diff between two skill versions."""

    asyncio.run(_diff_async(name, v1, v2))


@skills.command("rollback")
@click.argument("name")
@click.argument("version", type=int)
def skill_rollback(name: str, version: int) -> None:
    """Rollback a skill to a stored version."""

    asyncio.run(_rollback_async(name, version))


async def _manager() -> tuple[DatabaseManager, SkillManager]:
    settings = CrawlerSettings()
    db = DatabaseManager(settings.database_url)
    await db.init_db()
    return db, SkillManager(settings.crawler_skills_dir, db, "crawler")


async def _list_skills_async() -> None:
    db, manager = await _manager()
    try:
        for meta in manager.list_skills():
            click.echo(f"{meta.name}\tv{meta.version}\t{meta.description}")
    finally:
        await db.close()


async def _history_async(name: str) -> None:
    db, manager = await _manager()
    try:
        for item in await manager.get_history(name):
            marker = " current" if item.is_current else ""
            click.echo(f"v{item.version}\t{item.created_at.isoformat(timespec='seconds')}\t{item.change_summary}{marker}")
    finally:
        await db.close()


async def _diff_async(name: str, v1: int, v2: int) -> None:
    db, manager = await _manager()
    try:
        click.echo(await manager.diff_skill(name, v1, v2))
    finally:
        await db.close()


async def _rollback_async(name: str, version: int) -> None:
    db, manager = await _manager()
    try:
        await manager.rollback_skill(name, version)
        click.echo(f"Rolled back {name} to v{version}")
    finally:
        await db.close()

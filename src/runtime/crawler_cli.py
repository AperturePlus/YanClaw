from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher, FreshRunPreparationError
from agents.crawler.playwright_setup import ensure_chromium_installed
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger, setup_logging
from runtime.skills import SkillManager


@click.group()
def cli() -> None:
    """Yanclaw command line interface."""


@cli.command()
@click.option("--universities", default="", help="Comma-separated university names to crawl.")
@click.option("--concurrency", default=None, type=int, help="Override max concurrency.")
@click.option("--log-dir", default=None, type=click.Path(path_type=Path), help="Log directory.")
@click.option(
    "--fetcher-backend",
    default=None,
    type=click.Choice(["hybrid", "httpx", "playwright", "curl_cffi", "crawl4ai", "human"], case_sensitive=False),
    help="Fetcher backend: hybrid (default), httpx, playwright, curl_cffi, crawl4ai, or human (browser-assisted).",
)
@click.option(
    "--university-timeout-seconds",
    default=None,
    type=float,
    help="Per-university crawl timeout (seconds).",
)
@click.option(
    "--run-timeout-seconds",
    default=None,
    type=float,
    help="Total crawl timeout for this CLI invocation (seconds).",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Resume from existing per-university DB files instead of backing up and rebuilding them.",
)
@click.option("--skip-llm-check", is_flag=True, help="Skip the startup LLM connectivity check.")
@click.option(
    "--run-steward-after-crawl",
    is_flag=True,
    help="Run Data Steward after crawl completes.",
)
@click.option(
    "--steward-after-crawl-mode",
    default="dry_run",
    type=click.Choice(["dry_run", "apply"], case_sensitive=False),
    help="Data Steward mode when auto-run after crawl.",
)
def crawl(
    universities: str,
    concurrency: int | None,
    log_dir: Path | None,
    fetcher_backend: str | None,
    university_timeout_seconds: float | None,
    run_timeout_seconds: float | None,
    resume: bool,
    skip_llm_check: bool,
    run_steward_after_crawl: bool,
    steward_after_crawl_mode: str,
) -> None:
    """Start crawler agents."""

    overrides: dict[str, object] = {}
    if concurrency is not None:
        overrides["max_concurrency"] = concurrency
    if log_dir is not None:
        overrides["log_dir"] = log_dir
    if fetcher_backend is not None:
        overrides["fetcher_backend"] = fetcher_backend
    if university_timeout_seconds is not None:
        overrides["university_timeout_seconds"] = university_timeout_seconds
    settings = CrawlerSettings().model_copy(update=overrides) if overrides else CrawlerSettings()
    setup_logging(settings.log_dir)
    logger = get_logger("crawler.cli")
    selected = [item.strip() for item in universities.split(",") if item.strip()] or None
    logger.info(
        "Crawler config concurrency=%s fetcher_backend=%s university_timeout_seconds=%s request_timeout_seconds=%s llm_timeout_seconds=%s resume=%s selected=%s",
        settings.max_concurrency,
        settings.fetcher_backend,
        settings.university_timeout_seconds,
        settings.request_timeout_seconds,
        settings.llm_timeout_seconds,
        resume,
        ",".join(selected) if selected else "*",
    )
    if str(settings.fetcher_backend).strip().lower() == "playwright":
        try:
            installed_now = ensure_chromium_installed()
        except Exception as error:
            raise click.ClickException(f"Failed to prepare Playwright Chromium: {error}") from error
        if installed_now:
            logger.info("Installed Playwright Chromium runtime")
    asyncio.run(
        _crawl_async(
            settings,
            selected,
            skip_llm_check=skip_llm_check,
            run_timeout_seconds=run_timeout_seconds,
            resume=resume,
            run_steward_after_crawl=run_steward_after_crawl,
            steward_after_crawl_mode=steward_after_crawl_mode,
        )
    )


async def _crawl_async(
    settings: CrawlerSettings,
    universities: list[str] | None,
    *,
    skip_llm_check: bool = False,
    run_timeout_seconds: float | None = None,
    resume: bool = False,
    run_steward_after_crawl: bool = False,
    steward_after_crawl_mode: str = "dry_run",
) -> None:
    if not skip_llm_check:
        await _check_llm(settings)

    dispatcher = CrawlDispatcher(settings=settings)
    try:
        if run_timeout_seconds is None:
            summary = await dispatcher.run(universities, resume=resume)
        else:
            summary = await asyncio.wait_for(
                dispatcher.run(universities, resume=resume),
                timeout=float(run_timeout_seconds),
            )
    except asyncio.TimeoutError:
        raise click.ClickException(f"Total run timeout after {run_timeout_seconds}s")
    except ImportError as error:
        raise click.ClickException(str(error)) from error
    except FreshRunPreparationError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"success={summary.success} failed={summary.failed} skipped={summary.skipped}")
    if run_steward_after_crawl:
        steward_summary = await _steward_run_async(
            settings=settings,
            universities=universities,
            universities_file=None,
            db_roots=None,
            apply=steward_after_crawl_mode.strip().lower() == "apply",
            llm_enabled=False,
            max_context_tokens=256000,
            include_backup_audit=False,
        )
        click.echo(
            "steward "
            + f"targets={len(steward_summary.targets)} "
            + f"duplicates={steward_summary.total_duplicates_detected} "
            + f"deleted={steward_summary.total_duplicates_deleted} "
            + f"missing_audits={steward_summary.total_missing_field_audits}"
        )


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
def cookie() -> None:
    """Manage per-university cookies for WAF bypass."""


@cookie.command("import")
@click.argument("url", type=str)
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def cookie_import(url: str, file: Path) -> None:
    """Import cookies from a JSON file for a university URL.

    URL is the university start URL (e.g. https://www.scu.edu.cn/).
    FILE is a JSON file containing a list of cookie dicts.
    """
    import json as _json

    from agents.crawler.cookies import save_cookies

    data = _json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise click.ClickException("Cookie file must contain a JSON array of cookie objects")
    path = save_cookies(url, data)
    click.echo(f"Saved {len(data)} cookies to {path}")


@cookie.command("list")
def cookie_list() -> None:
    """List all stored cookie files."""
    from agents.crawler.cookies import list_cookie_files

    entries = list_cookie_files()
    if not entries:
        click.echo("No cookies stored.")
        return
    for domain, count in entries:
        click.echo(f"{domain}\t{count} cookies")


@cookie.command("clear")
@click.argument("url", type=str)
def cookie_clear(url: str) -> None:
    """Remove stored cookies for a university URL."""
    from agents.crawler.cookies import clear_cookies

    if clear_cookies(url):
        click.echo("Cookies removed.")
    else:
        click.echo("No cookies found for this URL.")


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


@cli.group()
def steward() -> None:
    """Run data stewardship cleanup and audits."""


@steward.command("run")
@click.option("--universities", default="", help="Comma-separated university names.")
@click.option("--universities-file", default=None, type=click.Path(exists=True, path_type=Path), help="File with one university name per line.")
@click.option("--db-roots", default="", help="Comma-separated DB roots (e.g. pku.edu.cn,tsinghua.edu.cn).")
@click.option("--apply", is_flag=True, help="Apply mutations (delete/update/enqueue). Default is dry-run.")
@click.option("--llm-enabled", is_flag=True, help="Enable LLM classification for uncertain missing-field reasons.")
@click.option("--max-context-tokens", default=128000, type=int, help="LLM context cap (hard-limited to <=256000).")
@click.option("--include-backup-audit", is_flag=True, help="Read-only compare with latest backup DB snapshot.")
def steward_run(
    universities: str,
    universities_file: Path | None,
    db_roots: str,
    apply: bool,
    llm_enabled: bool,
    max_context_tokens: int,
    include_backup_audit: bool,
) -> None:
    if apply and include_backup_audit:
        raise click.ClickException("`--include-backup-audit` is read-only and cannot be combined with `--apply`.")

    settings = CrawlerSettings()
    setup_logging(settings.log_dir)
    university_items = _split_csv(universities)
    root_items = _split_csv(db_roots)
    summary = asyncio.run(
        _steward_run_async(
            settings=settings,
            universities=university_items or None,
            universities_file=universities_file,
            db_roots=root_items or None,
            apply=apply,
            llm_enabled=llm_enabled,
            max_context_tokens=max_context_tokens,
            include_backup_audit=include_backup_audit,
        )
    )
    click.echo(
        "mode="
        + summary.mode
        + f" targets={len(summary.targets)} "
        + f"duplicates={summary.total_duplicates_detected} "
        + f"deleted={summary.total_duplicates_deleted} "
        + f"missing_audits={summary.total_missing_field_audits} "
        + f"recrawl_tasks={summary.total_recrawl_tasks_upserted}"
    )
    if summary.unmatched_universities:
        click.echo("unmatched_universities=" + ",".join(summary.unmatched_universities))
    if summary.unmatched_db_roots:
        click.echo("unmatched_db_roots=" + ",".join(summary.unmatched_db_roots))
    click.echo(json.dumps([run.__dict__ for run in summary.runs], ensure_ascii=False))


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


async def _steward_run_async(
    *,
    settings: CrawlerSettings,
    universities: list[str] | None,
    universities_file: Path | None,
    db_roots: list[str] | None,
    apply: bool,
    llm_enabled: bool,
    max_context_tokens: int,
    include_backup_audit: bool,
):
    from agents.data_steward.agent import DataStewardAgent

    agent = DataStewardAgent(settings=settings)
    return await agent.run(
        universities=universities,
        universities_file=universities_file,
        db_roots=db_roots,
        apply=apply,
        llm_enabled=llm_enabled,
        max_context_tokens=max_context_tokens,
        include_backup_audit=include_backup_audit,
    )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]

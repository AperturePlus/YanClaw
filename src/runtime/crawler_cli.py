from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher, FreshRunPreparationError
from runtime.llm import LLMClient
from runtime.logger import get_logger, setup_logging
from runtime.skills import SkillManager


@click.group()
def cli() -> None:
    """Yanclaw command line interface."""


@cli.command()
@click.option("--universities", default="", help="Comma-separated university names to crawl.")
@click.option("--org-units", default="", help="Comma-separated org-unit names to target with fuzzy matching.")
@click.option(
    "--org-unit-match-threshold",
    default=None,
    type=float,
    help="Fuzzy-match threshold in [0,1] for --org-units.",
)
@click.option(
    "--no-org-unit-exclude",
    is_flag=True,
    help="Disable default org-unit exclusion for arts/sports/joint programs/basic teaching units.",
)
@click.option(
    "--org-unit-exclude-keywords",
    default="",
    help="Comma-separated org-unit exclusion keywords overriding the defaults.",
)
@click.option(
    "--no-org-unit-llm-filter",
    is_flag=True,
    help="Disable LLM second-pass org-unit exclusion filtering.",
)
@click.option("--concurrency", default=None, type=int, help="Override max concurrency.")
@click.option("--log-dir", default=None, type=click.Path(path_type=Path), help="Log directory.")
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
    org_units: str,
    org_unit_match_threshold: float | None,
    no_org_unit_exclude: bool,
    org_unit_exclude_keywords: str,
    no_org_unit_llm_filter: bool,
    concurrency: int | None,
    log_dir: Path | None,
    university_timeout_seconds: float | None,
    run_timeout_seconds: float | None,
    resume: bool,
    skip_llm_check: bool,
    run_steward_after_crawl: bool,
    steward_after_crawl_mode: str,
) -> None:
    """Start crawler agents."""

    overrides: dict[str, object] = {}
    target_org_units = [item.strip() for item in org_units.split(",") if item.strip()]
    if target_org_units:
        overrides["target_org_units"] = target_org_units
    if org_unit_match_threshold is not None:
        overrides["org_unit_match_threshold"] = org_unit_match_threshold
    if no_org_unit_exclude:
        overrides["org_unit_exclude_enabled"] = False
    exclude_keywords = [item.strip() for item in org_unit_exclude_keywords.split(",") if item.strip()]
    if exclude_keywords:
        overrides["org_unit_exclude_keywords"] = exclude_keywords
    if no_org_unit_llm_filter:
        overrides["org_unit_llm_filter_enabled"] = False
    if concurrency is not None:
        overrides["max_concurrency"] = concurrency
    if log_dir is not None:
        overrides["log_dir"] = log_dir
    if university_timeout_seconds is not None:
        overrides["university_timeout_seconds"] = university_timeout_seconds
    settings = CrawlerSettings().model_copy(update=overrides) if overrides else CrawlerSettings()
    setup_logging(settings.log_dir)
    logger = get_logger("crawler.cli")
    selected = [item.strip() for item in universities.split(",") if item.strip()] or None
    logger.info(
        "Crawler config concurrency=%s human_bridge=%s:%s human_job_timeout_seconds=%s university_timeout_seconds=%s llm_timeout_seconds=%s resume=%s selected=%s target_org_units=%s org_unit_match_threshold=%s org_unit_exclude_enabled=%s org_unit_llm_filter_enabled=%s",
        settings.max_concurrency,
        settings.human_server_host,
        settings.human_server_port,
        settings.human_job_timeout_seconds,
        settings.university_timeout_seconds,
        settings.llm_timeout_seconds,
        resume,
        ",".join(selected) if selected else "*",
        ",".join(settings.target_org_units) if settings.target_org_units else "*",
        settings.org_unit_match_threshold,
        settings.org_unit_exclude_enabled,
        settings.org_unit_llm_filter_enabled,
    )
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
            llm_enabled=_resolve_steward_llm_enabled(settings, None),
            max_context_tokens=256000,
            include_backup_audit=False,
        )
        click.echo(
            "steward "
            + f"targets={len(steward_summary.targets)} "
            + f"duplicates={steward_summary.total_duplicates_detected} "
            + f"deleted={steward_summary.total_duplicates_deleted} "
            + f"excluded_org_units={getattr(steward_summary, 'total_excluded_org_units_detected', 0)} "
            + f"excluded_org_units_deleted={getattr(steward_summary, 'total_excluded_org_units_deleted', 0)} "
            + f"sub_department_sections={getattr(steward_summary, 'total_sub_department_sections_detected', 0)} "
            + f"sub_department_sections_merged={getattr(steward_summary, 'total_sub_department_sections_merged', 0)} "
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
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        seed=settings.llm_seed,
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
    click.echo(f"temperature={settings.llm_temperature}")
    click.echo(f"top_p={settings.llm_top_p}")
    click.echo(f"seed={settings.llm_seed}")
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
@click.option(
    "--agent",
    "skills_agent",
    default="crawler",
    type=click.Choice(["crawler", "data_steward"], case_sensitive=False),
    help="Skill owner to manage. Defaults to crawler.",
)
@click.pass_context
def skills(ctx: click.Context, skills_agent: str) -> None:
    """Manage agent skills."""

    ctx.obj = {**(ctx.obj or {}), "skills_agent": skills_agent.strip().lower()}


@skills.command("list")
@click.pass_context
def list_skills(ctx: click.Context) -> None:
    """List skills."""

    asyncio.run(_list_skills_async(_skills_agent_from_context(ctx)))


@cli.group()
def steward() -> None:
    """Run data stewardship cleanup and audits."""


@cli.group()
def graph() -> None:
    """Build and inspect the local recommendation knowledge graph."""


@graph.command("build")
@click.option("--universities", default="", help="Comma-separated university names.")
@click.option("--db-roots", default="", help="Comma-separated DB roots (e.g. pku.edu.cn,tsinghua.edu.cn).")
@click.option("--rebuild", is_flag=True, help="Clear the graph DB and rebuild selected sources.")
def graph_build(universities: str, db_roots: str, rebuild: bool) -> None:
    """Build the local knowledge graph from per-university crawler DBs."""

    settings = CrawlerSettings()
    setup_logging(settings.log_dir)
    summary = asyncio.run(
        _graph_build_async(
            settings=settings,
            universities=_split_csv(universities) or None,
            db_roots=_split_csv(db_roots) or None,
            rebuild=rebuild,
        )
    )
    click.echo(
        f"graph_db={summary.graph_db_path} "
        f"sources={summary.source_count} "
        f"indexed={summary.indexed_sources} "
        f"skipped={summary.skipped_sources} "
        f"nodes={summary.nodes_written} "
        f"edges={summary.edges_written} "
        f"terms={summary.terms_written}"
    )
    if summary.errors:
        click.echo("errors=" + json.dumps(summary.errors, ensure_ascii=False))


@cli.command("recommend")
@click.option("--text", default=None, help="Natural language request or resume text.")
@click.option("--file", "input_file", default=None, type=click.Path(exists=True, path_type=Path), help="Input .txt/.md/.pdf/.docx resume file.")
@click.option("--top-schools", default=None, type=int, help="Number of schools to return.")
@click.option("--top-org-units", default=None, type=int, help="Number of org units / directions to return.")
@click.option("--top-professors", default=None, type=int, help="Number of advisors to return.")
@click.option("--json", "json_output", is_flag=True, help="Output stable JSON.")
@click.option("--no-auto-build", is_flag=True, help="Do not auto-build the graph when missing or empty.")
def recommend(
    text: str | None,
    input_file: Path | None,
    top_schools: int | None,
    top_org_units: int | None,
    top_professors: int | None,
    json_output: bool,
    no_auto_build: bool,
) -> None:
    """Recommend schools, org units/directions, and advisors."""

    if bool(text and text.strip()) == bool(input_file):
        raise click.ClickException("Provide exactly one of --text or --file.")
    settings = CrawlerSettings()
    setup_logging(settings.log_dir)
    try:
        result = asyncio.run(
            _recommend_async(
                settings=settings,
                text=text,
                input_file=input_file,
                top_schools=top_schools,
                top_org_units=top_org_units,
                top_professors=top_professors,
                auto_build=not no_auto_build,
            )
        )
    except Exception as error:
        raise click.ClickException(str(error)) from error

    if json_output:
        click.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    click.echo(_format_recommendation_text(result))


@steward.command("run")
@click.option("--universities", default="", help="Comma-separated university names.")
@click.option("--universities-file", default=None, type=click.Path(exists=True, path_type=Path), help="File with one university name per line.")
@click.option("--db-roots", default="", help="Comma-separated DB roots (e.g. pku.edu.cn,tsinghua.edu.cn).")
@click.option("--apply", is_flag=True, help="Apply mutations (delete/update/enqueue). Default is dry-run.")
@click.option(
    "--llm-enabled/--no-llm",
    default=None,
    help="Enable or disable DataSteward LLM cleanup. Default: enabled when API key is set.",
)
@click.option("--max-context-tokens", default=128000, type=int, help="LLM context cap (hard-limited to <=256000).")
@click.option("--include-backup-audit", is_flag=True, help="Read-only compare with latest backup DB snapshot.")
def steward_run(
    universities: str,
    universities_file: Path | None,
    db_roots: str,
    apply: bool,
    llm_enabled: bool | None,
    max_context_tokens: int,
    include_backup_audit: bool,
) -> None:
    if apply and include_backup_audit:
        raise click.ClickException("`--include-backup-audit` is read-only and cannot be combined with `--apply`.")

    settings = CrawlerSettings()
    setup_logging(settings.log_dir)
    university_items = _split_csv(universities)
    root_items = _split_csv(db_roots)
    resolved_llm_enabled = _resolve_steward_llm_enabled(settings, llm_enabled)
    summary = asyncio.run(
        _steward_run_async(
            settings=settings,
            universities=university_items or None,
            universities_file=universities_file,
            db_roots=root_items or None,
            apply=apply,
            llm_enabled=resolved_llm_enabled,
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
        + f"excluded_org_units={getattr(summary, 'total_excluded_org_units_detected', 0)} "
        + f"excluded_org_units_deleted={getattr(summary, 'total_excluded_org_units_deleted', 0)} "
        + f"sub_department_sections={getattr(summary, 'total_sub_department_sections_detected', 0)} "
        + f"sub_department_sections_merged={getattr(summary, 'total_sub_department_sections_merged', 0)} "
        + f"missing_audits={summary.total_missing_field_audits} "
        + f"recrawl_tasks={summary.total_recrawl_tasks_upserted}"
    )
    if summary.unmatched_universities:
        click.echo("unmatched_universities=" + ",".join(summary.unmatched_universities))
    if summary.unmatched_db_roots:
        click.echo("unmatched_db_roots=" + ",".join(summary.unmatched_db_roots))
    click.echo(json.dumps([run.__dict__ for run in summary.runs], ensure_ascii=False))


def _skills_agent_from_context(ctx: click.Context) -> str:
    current: click.Context | None = ctx
    obj: dict[str, object] = {}
    while current is not None:
        if isinstance(current.obj, dict) and "skills_agent" in current.obj:
            obj = current.obj
            break
        current = current.parent
    value = str(obj.get("skills_agent") or "crawler").strip().lower()
    return value if value in {"crawler", "data_steward"} else "crawler"


def _skill_manager_for_settings(settings: CrawlerSettings, agent_name: str) -> SkillManager:
    if agent_name == "data_steward":
        return SkillManager(settings.data_steward_skills_dir)
    return SkillManager(settings.crawler_skills_dir)


async def _list_skills_async(agent_name: str = "crawler") -> None:
    manager = _skill_manager_for_settings(CrawlerSettings(), agent_name)
    for meta in manager.list_skills():
        applies_to = ",".join(meta.applies_to) if meta.applies_to else "*"
        allowed_tools = ",".join(meta.allowed_tools) if meta.allowed_tools else "-"
        token_budget = str(meta.token_budget) if meta.token_budget is not None else "-"
        click.echo(
            f"{meta.name}\tv{meta.version}\tpriority={meta.priority}\t"
            f"applies_to={applies_to}\tallowed_tools={allowed_tools}\t"
            f"token_budget={token_budget}\t{meta.description}"
        )


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


async def _graph_build_async(
    *,
    settings: CrawlerSettings,
    universities: list[str] | None,
    db_roots: list[str] | None,
    rebuild: bool,
):
    from agents.recommender.graph_agent import KnowledgeGraphAgent

    return await KnowledgeGraphAgent(settings=settings).build(
        universities=universities,
        db_roots=db_roots,
        rebuild=rebuild,
    )


async def _recommend_async(
    *,
    settings: CrawlerSettings,
    text: str | None,
    input_file: Path | None,
    top_schools: int | None,
    top_org_units: int | None,
    top_professors: int | None,
    auto_build: bool,
):
    from agents.recommender.agent import RecommendationAgent

    return await RecommendationAgent(settings=settings).recommend(
        text=text,
        file=input_file,
        top_schools=top_schools,
        top_org_units=top_org_units,
        top_professors=top_professors,
        auto_build=auto_build,
    )


def _format_recommendation_text(result) -> str:
    lines: list[str] = []
    profile = result.profile
    if profile.interests:
        lines.append("Interests: " + ", ".join(profile.interests[:10]))
    if profile.target_locations:
        lines.append("Target locations: " + ", ".join(profile.target_locations))
    lines.append("")
    lines.append("Schools")
    for index, item in enumerate(result.schools, start=1):
        location = f" ({item.location})" if item.location else ""
        lines.append(f"{index}. {item.university_name}{location} score={item.score:.2f}")
        if item.representative_org_units:
            lines.append("   org_units=" + ", ".join(item.representative_org_units[:5]))
    lines.append("")
    lines.append("Org Units / Directions")
    for index, item in enumerate(result.org_units, start=1):
        lines.append(f"{index}. {item.university_name} / {item.org_unit_name} score={item.score:.2f}")
        if item.representative_professors:
            lines.append("   advisors=" + ", ".join(item.representative_professors[:5]))
    lines.append("")
    lines.append("Advisors")
    for index, item in enumerate(result.professors, start=1):
        title = f" {item.title}" if item.title else ""
        lines.append(
            f"{index}. {item.university_name} / {item.org_unit_name} / {item.name}{title} "
            f"score={item.score:.2f}"
        )
        if item.research_areas:
            lines.append("   research=" + item.research_areas[:120])
        if item.reasons:
            lines.append("   reason=" + item.reasons[0])
    return "\n".join(lines).strip()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _resolve_steward_llm_enabled(settings: CrawlerSettings, override: bool | None) -> bool:
    if override is not None:
        return bool(override)
    return bool((settings.openai_api_key or "").strip())

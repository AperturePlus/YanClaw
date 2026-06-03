from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from runtime.database import Base


class CrawlStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CrawlLogStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class CrawlTaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RETRY = "retry"
    DONE = "done"
    FAILED = "failed"


class CrawlTaskKind(str, Enum):
    LIST_PAGE = "list_page"
    DETAIL_PAGE = "detail_page"


class OrgUnitStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_FACULTY_PAGE = "no_faculty_page"


class UniversityMeta(Base):
    """Per-university metadata stored inside each university DB."""

    __tablename__ = "university_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    start_url: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    crawl_status: Mapped[str] = mapped_column(String(32), default=CrawlStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class OrgUnit(Base):
    """Academic org units under a university: colleges/schools/departments/institutes."""

    __tablename__ = "org_units"
    __table_args__ = (
        UniqueConstraint("url", name="uq_org_unit_url"),
        UniqueConstraint("name", name="uq_org_unit_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=OrgUnitStatus.PENDING.value)
    discovered_from_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    affiliations: Mapped[list["ProfessorAffiliation"]] = relationship(
        back_populates="org_unit",
        cascade="all, delete-orphan",
    )


class Professor(Base):
    __tablename__ = "professors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    name_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    org_unit_name: Mapped[str] = mapped_column(String(255), default="Unknown", index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Internal profile page where the crawler got this professor info.
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional external/personal homepage mentioned inside the profile content.
    external_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrollment_pref: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    affiliations: Mapped[list["ProfessorAffiliation"]] = relationship(
        back_populates="professor",
        cascade="all, delete-orphan",
    )


class Academician(Base):
    __tablename__ = "academicians"
    __table_args__ = (
        UniqueConstraint("name", "org_unit_id", name="uq_academician_name_org_unit"),
        UniqueConstraint("org_unit_id", "name_key", name="uq_academician_org_unit_name_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    name_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Internal profile page where the crawler got this academician info.
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional external/personal homepage mentioned inside the profile content.
    external_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrollment_pref: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications: Mapped[str | None] = mapped_column(Text, nullable=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_units.id"), index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    org_unit: Mapped[OrgUnit] = relationship()


class ProfessorAffiliation(Base):
    __tablename__ = "professor_affiliations"
    __table_args__ = (
        UniqueConstraint("professor_id", "org_unit_id", name="uq_professor_affiliation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_units.id"), index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    professor: Mapped[Professor] = relationship(back_populates="affiliations")
    org_unit: Mapped[OrgUnit] = relationship(back_populates="affiliations")


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class CrawlTask(Base):
    __tablename__ = "crawl_tasks"
    __table_args__ = (
        UniqueConstraint("source_url", "org_unit_name", "page_hash", name="uq_crawl_task_dedup"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university: Mapped[str] = mapped_column(String(255), index=True, default="")
    org_unit_name: Mapped[str] = mapped_column(String(255), index=True)
    org_unit_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, index=True)
    page_url: Mapped[str] = mapped_column(Text, index=True)
    page_hash: Mapped[str] = mapped_column(String(64), index=True)
    task_kind: Mapped[str] = mapped_column(String(32), index=True, default=CrawlTaskKind.LIST_PAGE.value)
    page_text_snapshot: Mapped[str] = mapped_column(Text, default="")
    allowed_tools: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), index=True, default=CrawlTaskStatus.PENDING.value)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    failures: Mapped[list["CrawlExtractionFailure"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )


class CrawlExtractionFailure(Base):
    __tablename__ = "crawl_extraction_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_tasks.id"), nullable=True, index=True)
    failure_type: Mapped[str] = mapped_column(String(64), index=True)
    org_unit_name: Mapped[str] = mapped_column(String(255), index=True, default="")
    source_url: Mapped[str] = mapped_column(Text, index=True, default="")
    professor_name_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_arguments_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    resolver: Mapped[str] = mapped_column(String(32), default="dropped")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    task: Mapped[CrawlTask | None] = relationship(back_populates="failures")


class StewardRun(Base):
    __tablename__ = "steward_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="dry_run")
    target_db: Mapped[str] = mapped_column(String(255), default="")
    target_selectors: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DataQualityAudit(Base):
    __tablename__ = "data_quality_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("steward_runs.id"), nullable=True, index=True)
    db_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    issue_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    field_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(String(64), default="report_only")
    before_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    run: Mapped[StewardRun | None] = relationship()

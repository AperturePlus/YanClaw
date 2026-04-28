from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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


class OrgUnitStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


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
    __table_args__ = (UniqueConstraint("url", name="uq_org_unit_url"),)

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
    org_unit_name: Mapped[str] = mapped_column(String(255), default="Unknown", index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    __table_args__ = (UniqueConstraint("name", "org_unit_id", name="uq_academician_name_org_unit"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
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

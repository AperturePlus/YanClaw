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


class University(Base):
    __tablename__ = "universities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    url: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    crawl_status: Mapped[str] = mapped_column(String(32), default=CrawlStatus.PENDING.value)

    colleges: Mapped[list["College"]] = relationship(
        back_populates="university",
        cascade="all, delete-orphan",
    )
    crawl_logs: Mapped[list["CrawlLog"]] = relationship(
        back_populates="university",
        cascade="all, delete-orphan",
    )


class College(Base):
    __tablename__ = "colleges"
    __table_args__ = (UniqueConstraint("name", "university_id", name="uq_college_university"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    university_id: Mapped[int] = mapped_column(ForeignKey("universities.id"), index=True)

    university: Mapped[University] = relationship(back_populates="colleges")
    professors: Mapped[list["Professor"]] = relationship(
        back_populates="college",
        cascade="all, delete-orphan",
    )
    professor_affiliations: Mapped[list["ProfessorAffiliation"]] = relationship(
        back_populates="college",
        cascade="all, delete-orphan",
    )


class Professor(Base):
    __tablename__ = "professors"
    __table_args__ = (UniqueConstraint("name", "college_id", name="uq_professor_college"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_areas: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    homepage: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrollment_pref: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications: Mapped[str | None] = mapped_column(Text, nullable=True)
    college_id: Mapped[int] = mapped_column(ForeignKey("colleges.id"), index=True)

    college: Mapped[College] = relationship(back_populates="professors")
    affiliations: Mapped[list["ProfessorAffiliation"]] = relationship(
        back_populates="professor",
        cascade="all, delete-orphan",
    )


class ProfessorAffiliation(Base):
    __tablename__ = "professor_affiliations"
    __table_args__ = (
        UniqueConstraint("professor_id", "college_id", name="uq_professor_affiliation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    professor_id: Mapped[int] = mapped_column(ForeignKey("professors.id"), index=True)
    college_id: Mapped[int] = mapped_column(ForeignKey("colleges.id"), index=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    professor: Mapped[Professor] = relationship(back_populates="affiliations")
    college: Mapped[College] = relationship(back_populates="professor_affiliations")


class CrawlLog(Base):
    __tablename__ = "crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(ForeignKey("universities.id"), index=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    university: Mapped[University] = relationship(back_populates="crawl_logs")

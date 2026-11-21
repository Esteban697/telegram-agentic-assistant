"""Timing rules for the consolidated Cloud Run dispatcher."""

from __future__ import annotations

from datetime import datetime, timezone

from app.jobs.tick import selected_jobs


def at(minute: int) -> datetime:
    return datetime(2026, 11, 21, 12, minute, tzinfo=timezone.utc)


def test_every_tick_dispatches_reminders() -> None:
    assert selected_jobs(at(15)) == ("reminders",)
    assert selected_jobs(at(45)) == ("reminders",)


def test_half_hour_tick_adds_resume() -> None:
    assert selected_jobs(at(30)) == ("reminders", "resume")


def test_top_of_hour_adds_resume_and_documents() -> None:
    assert selected_jobs(at(0)) == ("reminders", "resume", "documents")

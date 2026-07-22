"""Content pipeline: CSV -> agent configs -> DB.

Standalone script (no UI). Reads the agents CSV and generates working agent
configs — system prompts, metadata, parent/sub-agent relationships — then
upserts them into the database. Idempotent: safe to re-run on an updated CSV
(rows are keyed by slug, so re-running updates rather than duplicates).

Usage:
    python -m app.pipeline.seed_agents [--csv PATH] [--llm-polish]

By default, prompts are generated from templates (deterministic, no API key).
Pass --llm-polish to additionally refine each prompt through the configured
LLM provider (requires a valid API key).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.pipeline.templates import MAIN_AGENT_TEMPLATE, SUB_AGENT_TEMPLATE
from app.repositories import agent_repo

# The 5 main agents we explicitly feature/verify (distinct personas). This is a
# DATA list (which slugs to spotlight), not per-agent branching code.
FEATURED_MAIN_SLUGS = {
    "doctor-physician",
    "corporate-lawyer",
    "software-developer",
    "financial-controller",
    "talent-acquisition-specialist",
}


def slugify(value: str) -> str:
    value = str(value).lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def build_main_prompt(industry: str, profession: str, tasks: list[str]) -> str:
    task_lines = "\n".join(f"- {t}" for t in tasks if t and str(t).strip())
    return MAIN_AGENT_TEMPLATE.format(
        profession=profession, industry=industry, task_lines=task_lines
    ).strip()


def build_sub_prompt(
    industry: str, profession: str, sub_name: str, sub_task: str
) -> str:
    return SUB_AGENT_TEMPLATE.format(
        sub_name=sub_name,
        sub_task=sub_task,
        profession=profession,
        industry=industry,
    ).strip()


def read_rows(csv_path: Path):
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for r in reader:
            if not r or not str(r[0]).strip():
                continue
            yield r


async def _maybe_make_polish_fn(use_polish: bool):
    """Return an optional async prompt-polishing function backed by the LLM."""
    if not use_polish:
        return None

    from app.services.llm import get_llm_provider
    from app.services.llm.base import LLMMessage

    provider = get_llm_provider()

    async def polish(draft: str, role: str) -> str:
        instruction = (
            "You refine SYSTEM PROMPTS for an AI assistant. Rewrite the prompt "
            f"below for an assistant that is {role}. Keep it in the second person, "
            "crisp, professional, behaviour-focused, and preserve all domain "
            "specifics. Return ONLY the improved system prompt — no commentary."
        )
        refined = await provider.complete(
            system=instruction,
            messages=[LLMMessage(role="user", content=draft)],
            max_tokens=900,
            temperature=0.3,
        )
        return refined.strip() or draft

    return polish


async def seed(
    db: AsyncSession,
    csv_path: Path,
    polish_fn=None,
) -> dict[str, int]:
    counts = {"main": 0, "sub": 0}

    for row in read_rows(csv_path):
        num, industry, profession = row[0], row[1], row[2]
        tasks = list(row[3:7])
        sub_names = list(row[7:11])
        sub_tasks = list(row[11:15])

        main_slug = slugify(profession)
        main_prompt = build_main_prompt(industry, profession, tasks)
        if polish_fn:
            main_prompt = await polish_fn(main_prompt, f"a senior {profession} in {industry}")

        main_agent = await agent_repo.upsert_agent(
            db,
            slug=main_slug,
            industry=industry,
            profession=profession,
            tagline=f"Your AI {profession} for {industry}",
            description=(
                f"An expert AI assistant for {profession}s working in the "
                f"{industry} sector, with specialised sub-agents for key tasks."
            ),
            system_prompt=main_prompt,
            parent_id=None,
            sort_order=int(num) if str(num).strip().isdigit() else 0,
            is_active=True,
            is_featured=main_slug in FEATURED_MAIN_SLUGS,
        )
        counts["main"] += 1

        for index, (sname, stask) in enumerate(zip(sub_names, sub_tasks), start=1):
            if not sname or not str(sname).strip():
                continue
            sub_slug = f"{main_slug}-{slugify(sname)}"
            sub_prompt = build_sub_prompt(industry, profession, sname, stask)
            if polish_fn:
                sub_prompt = await polish_fn(
                    sub_prompt, f"the {sname} specialisation for a {profession}"
                )
            await agent_repo.upsert_agent(
                db,
                slug=sub_slug,
                industry=industry,
                # A sub-agent's "profession" field holds its role/name.
                profession=sname,
                tagline=str(stask),
                description=str(stask),
                system_prompt=sub_prompt,
                parent_id=main_agent.id,
                sort_order=index,
                is_active=True,
                is_featured=False,
            )
            counts["sub"] += 1

    await db.commit()
    return counts


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed agents from CSV into the DB.")
    parser.add_argument("--csv", default=settings.AGENTS_CSV_PATH, help="Path to agents CSV.")
    parser.add_argument(
        "--llm-polish",
        action="store_true",
        help="Refine generated prompts through the LLM provider (needs API key).",
    )
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found at {csv_path}", file=sys.stderr)
        return 2

    polish_fn = await _maybe_make_polish_fn(args.llm_polish)

    async with AsyncSessionLocal() as db:
        counts = await seed(db, csv_path, polish_fn=polish_fn)

    print(
        f"Seeded {counts['main']} main agents and {counts['sub']} sub-agents "
        f"from {csv_path} ({'LLM-polished' if polish_fn else 'templated'})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

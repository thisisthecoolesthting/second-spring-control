"""OpenAI integration with parallel-batch capability.

Three call types:
  draft   -> fills a product review skeleton
  pillar  -> writes a 2,000-2,500 word pillar from a topic + product list
  safety  -> rewrites just the Safety section with extra care

Costs are recorded per request in ai_requests table for rolling visibility.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

from . import db
from .config import settings


# Approximate per-1k-token pricing (as of 2025). Update if you change models.
MODEL_PRICING = {
    "gpt-4o-mini":      {"input": 0.00015, "output": 0.0006},
    "gpt-4o":           {"input": 0.005,   "output": 0.015},
    "gpt-4-turbo":      {"input": 0.010,   "output": 0.030},
    "gpt-3.5-turbo":    {"input": 0.0005,  "output": 0.0015},
}


SYSTEM_PROMPT = """\
You are the in-house writer for Second Spring Club, a product review and \
guidance site for women in perimenopause, menopause, and the years after.

Voice rules:
- Warm, candid, specific. You've been through this or watched someone close \
go through it. No clinical detachment, no cheerleading.
- First-person plural ("we tested", "we found"). Never "I".
- Short sentences alongside medium ones. One-sentence paragraphs are fine.
- No emoji. No exclamation points. No "game-changer", "revolutionary", \
"must-have", "life-changing", "transformative", "ultimate".
- Speak to the reader as an adult.
- When you don't know something, say so. Never invent clinical claims, \
study results, or medical interactions.

Content rules:
- For ingestible/topical products, include a Safety section with honest \
interaction notes. Cite drugs.com, NIH, or Examine.com rather than inventing.
- Don't quote exact dollar prices in prose; use "around $X" or "mid-$X range".
- End with a clear recommendation: Great pick / Good but has caveats / Skip.
- Length is a constraint, not a target. Fewer accurate words beat padded ones.
"""


# ---------------------------------------------------------------------------
# Prompt renderers
# ---------------------------------------------------------------------------

DRAFT_INSTRUCTIONS = """\
Fill every <!-- SECTION ... --> block in the Markdown below. Keep the \
frontmatter exactly as-is, but fill the empty `seoTitle` and \
`metaDescription` values (155-char max for description; SEO title ends \
with " | Second Spring Club").

Follow each SECTION's Prompt instruction. Don't change section headings, \
don't add sections, don't wrap in code fences.

After writing each section, REMOVE the HTML comment above it so the final \
output is clean publishable Markdown.

Skeleton:

{skeleton}
"""


PILLAR_INSTRUCTIONS = """\
Write a 2,000-2,500 word pillar page titled "{title}".

Target reader: a woman 42-58 actively searching for solutions to \
"{problem_statement}". She has tried things that didn't work and wants a \
trustworthy source that treats her like an adult.

Structure:
1. 120-word intro that names the problem and her frustration with bad advice.
2. "What's actually happening in your body" — 250-word plain-English \
explainer cited in NIH or Menopause Society style.
3. "What to try first" — 3-5 specific non-product interventions with \
evidence sentence each.
4. "The products worth considering" — 400-word section linking to:
{product_links}
5. "When to see a doctor" — 150-word section with red flags.
6. "The bottom line" — 100-word close with 3 takeaways.

Include a Safety section if relevant.

Target keyword: "{target_keyword}". Use naturally in H1, first 100 words, \
one H2, conclusion. Do not stuff.

Return Markdown with this frontmatter, then the article:

---
title: "{title}"
slug: "{slug}"
metaDescription: "[155 CHAR MAX]"
publishedAt: {today}
excerpt: "[80 CHAR SUMMARY]"
targetKeyword: "{target_keyword}"
relatedProducts: {related_asins}
status: "draft"
tags: ["menopause"]
---
"""


SAFETY_INSTRUCTIONS = """\
Write a 100-150 word "Safety and interactions" section for "{product_name}", \
which contains "{active_ingredient}".

Include known drug interactions for the ingredient (drugs.com, NIH), \
conditions warranting doctor consultation (pregnancy, blood thinners, \
specific cancers, thyroid), typical dosing range, and one explicit \
"talk to your doctor" sentence.

If non-ingestible (pillow, pajamas, book, exercise gear), return only:
"No interactions apply — this product is not ingested or applied topically \
in a way that could interact with medications."

Never invent interactions. If unsure, say "evidence is limited."

Return plain Markdown, no headers, no fences.
"""


def render_draft_prompt(skeleton_md: str) -> str:
    return DRAFT_INSTRUCTIONS.format(skeleton=skeleton_md)


def render_pillar_prompt(
    title: str,
    slug: str,
    problem_statement: str,
    target_keyword: str,
    product_slugs: list[str],
    related_asins: list[str],
    today_iso: str,
) -> str:
    links = "\n".join(f"- [{s}](/products/{s})" for s in product_slugs)
    return PILLAR_INSTRUCTIONS.format(
        title=title,
        slug=slug,
        problem_statement=problem_statement,
        target_keyword=target_keyword,
        product_links=links,
        related_asins=related_asins,
        today=today_iso,
    )


def render_safety_prompt(product_name: str, active_ingredient: str) -> str:
    return SAFETY_INSTRUCTIONS.format(
        product_name=product_name,
        active_ingredient=active_ingredient or "the product's primary component",
    )


# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------

@dataclass
class AIResult:
    ok: bool
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None


def _client():
    if not settings.openai_configured:
        raise RuntimeError("OPENAI_API_KEY not set in .env")
    # Imported lazily so the rest of the app can import this module
    # without OpenAI installed (e.g., in tests).
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=settings.openai_api_key)


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model, {"input": 0.001, "output": 0.003})
    return (
        prompt_tokens / 1000 * pricing["input"]
        + completion_tokens / 1000 * pricing["output"]
    )


async def _call(
    model: str,
    user_prompt: str,
    item_id: int | None = None,
    purpose: str = "draft",
    system_prompt: str = SYSTEM_PROMPT,
    temperature: float = 0.7,
    max_tokens: int = 4000,
) -> AIResult:
    started = time.monotonic()
    request_id = db.execute(
        "INSERT INTO ai_requests (item_id, purpose, model, system_prompt, user_prompt, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (item_id, purpose, model, system_prompt, user_prompt),
    )

    try:
        client = _client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        cost = _cost(model, prompt_tokens, completion_tokens)
        duration_ms = int((time.monotonic() - started) * 1000)

        db.execute(
            "UPDATE ai_requests SET response_md = ?, prompt_tokens = ?, "
            "completion_tokens = ?, cost_usd = ?, duration_ms = ?, status = 'ok' "
            "WHERE id = ?",
            (text, prompt_tokens, completion_tokens, cost, duration_ms, request_id),
        )
        return AIResult(
            ok=True,
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            duration_ms=duration_ms,
        )
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        db.execute(
            "UPDATE ai_requests SET status = 'error', error = ?, duration_ms = ? "
            "WHERE id = ?",
            (str(e), duration_ms, request_id),
        )
        return AIResult(ok=False, text="", duration_ms=duration_ms, error=str(e))


async def draft_one(item_id: int, skeleton_md: str) -> AIResult:
    return await _call(
        model=settings.openai_model_draft,
        user_prompt=render_draft_prompt(skeleton_md),
        item_id=item_id,
        purpose="draft",
    )


async def safety_one(item_id: int, product_name: str, active_ingredient: str = "") -> AIResult:
    return await _call(
        model=settings.openai_model_safety,
        user_prompt=render_safety_prompt(product_name, active_ingredient),
        item_id=item_id,
        purpose="safety",
        max_tokens=600,
    )


async def pillar_one(item_id: int, **kwargs) -> AIResult:
    return await _call(
        model=settings.openai_model_pillar,
        user_prompt=render_pillar_prompt(**kwargs),
        item_id=item_id,
        purpose="pillar",
        max_tokens=6000,
    )


async def draft_batch(items: Iterable[tuple[int, str]]) -> List[AIResult]:
    """Fire up to MAX_PARALLEL_DRAFTS at once. Returns results in input order."""
    sem = asyncio.Semaphore(settings.max_parallel_drafts)

    async def _bounded(item_id: int, skeleton_md: str) -> AIResult:
        async with sem:
            return await draft_one(item_id, skeleton_md)

    return await asyncio.gather(*(_bounded(i, s) for i, s in items))


# ---------------------------------------------------------------------------
# Brainstorm + SEO helpers
# ---------------------------------------------------------------------------

BRAINSTORM_INSTRUCTIONS = """\
Generate exactly {n} content ideas for Second Spring Club on the topic:
"{topic}"

Mix the idea types roughly:
  - {n_pillars} PILLAR ideas (long 2,000+ word authority guides)
  - {n_articles} ARTICLE ideas (600-800 word Q&A / how-to)
  - {n_products} PRODUCT category ideas (things to source on Amazon)

Each idea must be:
- Specific and searchable, not generic. "Best cooling pillow for night sweats \
in your 40s" — not "good pillows".
- Rooted in a real problem a woman 42-58 would search for.
- Free of forbidden words (game-changer, ultimate, must-have, revolutionary).

Return a JSON array, no prose, no code fences. Each entry:
{{
  "title": "...",
  "type": "pillar" | "article" | "product",
  "target_keyword": "...",
  "notes": "why this matters / who this is for, one sentence"
}}
"""


META_INSTRUCTIONS = """\
Write a 155-character-max meta description for an article titled:
"{title}"

Include primary benefit. Feel human. End with a subtle reason to click. \
No exclamation. No "everything you need to know". No "ultimate guide".

Return only the single line of text.
"""


SEO_TITLE_INSTRUCTIONS = """\
Write an SEO-optimized title under 65 characters for:
"{title}"

It must include the main keyword naturally and end with " | Second Spring Club". \
Return only the single line.
"""


def render_brainstorm_prompt(topic: str, n: int = 20) -> str:
    n_pillars = max(1, n // 6)
    n_products = max(2, n // 3)
    n_articles = n - n_pillars - n_products
    return BRAINSTORM_INSTRUCTIONS.format(
        topic=topic, n=n,
        n_pillars=n_pillars, n_articles=n_articles, n_products=n_products,
    )


async def brainstorm_ideas(topic: str, n: int = 20) -> AIResult:
    return await _call(
        model=settings.openai_model_pillar,  # use the better model for research
        user_prompt=render_brainstorm_prompt(topic, n),
        purpose="brainstorm",
        max_tokens=3000,
        temperature=0.9,  # higher creativity for ideation
    )


async def generate_meta(item_id: int, title: str) -> AIResult:
    return await _call(
        model=settings.openai_model_draft,
        user_prompt=META_INSTRUCTIONS.format(title=title),
        item_id=item_id,
        purpose="meta",
        max_tokens=200,
        temperature=0.6,
    )


async def generate_seo_title(item_id: int, title: str) -> AIResult:
    return await _call(
        model=settings.openai_model_draft,
        user_prompt=SEO_TITLE_INSTRUCTIONS.format(title=title),
        item_id=item_id,
        purpose="seo_title",
        max_tokens=80,
        temperature=0.4,
    )


# ---------------------------------------------------------------------------
# Auto-QA: AI critiques its own draft so humans don't have to.
# ---------------------------------------------------------------------------

CRITIQUE_INSTRUCTIONS = """\
You are the senior editor at Second Spring Club. Review the draft product
review below. Score it 0-10 against the voice and content rules you enforce.

Return STRICT JSON ONLY, no prose, no code fences. Schema:
{
  "score": <0-10>,
  "pass": <true if score >= 7 AND no critical issues>,
  "issues": [
    {"severity": "critical"|"major"|"minor",
     "where": "short quote from the draft",
     "why": "what's wrong",
     "fix": "concrete rewrite suggestion"}
  ],
  "rewrite": "<full clean replacement draft — ONLY include if score < 7>"
}

Critical issues (score must be <= 4 if any present):
- Contains any of: game-changer, game changer, revolutionary, must-have,
  must have, life-changing, life changing, transformative, ultimate guide,
  look no further, unleash, dive into, cutting-edge, world-class,
  in today's fast-paced world
- Uses "I" (should be "we")
- Uses emoji or exclamation points
- Any leftover <!-- SECTION markers
- Invented clinical claims or made-up statistics
- Missing affiliate CTA / Amazon link
- Missing required Safety section for ingestible/topical products

Major issues (score 5-6):
- Generic filler paragraphs ("listen to your body", "everyone is different")
- Exact dollar prices quoted in prose instead of "around $X"
- Weak or weasel-worded recommendation ("might be good for some people")
- Obvious AI tells (overuse of "nuanced", "comprehensive", "holistic")

Minor issues (score 7-8):
- Awkward sentence rhythm
- Missed opportunity for a specific anecdote
- Meta description or SEO title empty in frontmatter

Score 9-10: reads like a human wrote it, specific, honest, useful.

Here is the draft to critique:

<<<DRAFT>>>
{draft}
<<<END DRAFT>>>
"""


async def critique_draft(item_id: int, draft_md: str) -> AIResult:
    """Score a draft and optionally auto-rewrite if bad. Returns the raw
    AI response which should parse as JSON.

    Uses .replace() instead of .format() because the prompt contains a
    literal JSON example with braces, which str.format() would mis-parse.
    """
    user_prompt = CRITIQUE_INSTRUCTIONS.replace("{draft}", draft_md)
    return await _call(
        model=settings.openai_model_safety,  # use the better model for critique
        user_prompt=user_prompt,
        item_id=item_id,
        purpose="critique",
        max_tokens=4000,
        temperature=0.3,  # lower temp for consistent scoring
    )

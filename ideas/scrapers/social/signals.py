"""Rule-based pain detection: turn a raw post into a scored `PainPoint`.

Deliberately deterministic and LLM-free. Roughly 95% of any social firehose is
not a problem statement, and paying a model to read that is slow, expensive and
unreproducible. This pass throws the bulk away for free; a later LLM pass only
ever sees what survives here, and overwrites `problem_statement` / `who` /
`domain` with something better.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from ..models import PainPoint
from .base import SocialPost, clean

# ---------------------------------------------------------------------------
# pain phrases, weighted by how strongly they imply an *unmet* need
# ---------------------------------------------------------------------------
PAIN_PATTERNS: list[tuple[int, str, str]] = [
    # 3 - someone is explicitly asking for a product that does not exist
    (3, "would-pay",        r"\b(?:i'?d|i would|we'?d|we would) (?:happily )?pay\b|\bwilling to pay\b|\btake my money\b"),
    (3, "why-no-tool",      r"\bwhy (?:is|are) there no\b|\bwhy isn'?t there\b|\bwhy doesn'?t (?:anyone|someone|something) \w+"),
    (3, "wish-existed",     r"\b(?:i|we) wish there (?:was|were)\b|\bsomeone should (?:build|make)\b|\bwould love a tool\b"),
    # "is there any *way*" is too broad - it catches "any way to escalate this to
    # support", which is a customer-service question, not an unmet product need.
    (3, "looking-for-tool", r"\bis there (?:a|any) (?:good )?(?:tool|app|service|software|library|product|alternative)\b|"
                            r"\banyone know (?:of )?a (?:tool|app|service)\b|\blooking for a tool\b|"
                            r"\bis there (?:a|any) way to automate\b"),
    (3, "biggest-pain",     r"\bbiggest (?:pain|problem|headache|frustration)\b|\bsingle biggest\b"),

    # 2 - a concrete, named frustration
    (2, "no-good-option",   r"\bno good (?:tool|solution|option|alternative)\b|\bnothing (?:out there|exists)\b"),
    (2, "no-easy-way",      r"\bno easy way to\b|\bthere'?s no way to\b|\bcan'?t find a (?:tool|way|solution)\b"),
    (2, "wasting-time",     r"\bwast(?:e|es|ing) (?:hours|days|weeks|so much time|my time)\b|\bhours (?:every|each) (?:day|week)\b"),
    (2, "manual-toil",      r"\bmanual(?:ly)?\b|\bby hand\b|\bcopy[- ]?paste\b|\bspreadsheet hell\b"),
    (2, "hate-it",          r"\bi hate (?:that|having to|when)\b|\bpain in the (?:ass|neck)\b|\bdrives me (?:crazy|nuts)\b|\bso frustrating\b"),
    (2, "workaround",       r"\bworkaround\b|\bhacky (?:script|solution)\b|\bduct[- ]taped?\b"),
    # App-store vocabulary: a review rarely says "pain point", it says the app
    # will not let you do the thing. Both are the same missing capability.
    (3, "missing-option",   r"\bno (?:option|setting|feature|button) to\b|\bdoesn'?t (?:let|allow) (?:me|you|us)\b|"
                            r"\bwon'?t let (?:me|you|us)\b|\bwish (?:you could|i could|it could)\b"),
    (2, "regression",       r"\bused to (?:be able to|work|let|have)\b|\bnow you (?:have to|can'?t)\b"),

    # 1 - weak grumbling; only counts alongside something stronger or high engagement
    (1, "tedious",          r"\btedious\b|\bcumbersome\b|\bclunky\b|\bannoying\b|\bpainful\b"),
    (1, "struggle",         r"\bstruggl(?:e|es|ing) (?:with|to)\b|\bhard to keep track\b|\bdifficult to\b"),
    (1, "repetitive",       r"\btakes forever\b|\bover and over\b|\bevery single time\b|\bagain and again\b"),
    (1, "legacy-tooling",   r"\bstill (?:using|on) (?:excel|spreadsheets|paper|fax)\b|\bexcel sheet\b"),
]

COMPILED = [(w, label, re.compile(rx, re.I)) for w, label, rx in PAIN_PATTERNS]

#: The weight-3 families as literal phrases, for sources whose own search can do
#: the filtering server-side. Downloading a firehose and discarding 99% of it is
#: only necessary when the platform gives you no way to ask for the 1%.
SERVER_SIDE_PHRASES = (
    "wish there was",
    "why is there no",
    "is there a tool",
    "biggest pain",
    "I would pay",
    "no easy way",
)

#: posts that structurally cannot be a problem statement
NOISE = re.compile(
    r"^\s*(?:show hn|launch hn|tell hn: |ask hn: who is hiring|who is hiring|"
    r"freelancer\?? seeking freelancer|who wants to be hired)",
    re.I,
)

MIN_CHARS = 60

# who is hurting - only fires on an explicit self-identification
WHO = re.compile(
    r"\b(?:as an?|i'?m an?|i am an?|we'?re an?|we are an?|our team of)\s+"
    r"([a-z][a-z /\-]{2,34}?)(?=[,.;:]|\s+(?:and|who|that|i|we|at|in)\b)",
    re.I,
)

DOMAINS: list[tuple[str, str]] = [
    ("devtools",     r"\b(?:ci/cd|kubernetes|docker|deploy|api|sdk|compiler|linter|repo|git|terraform|observability|logging)\b"),
    ("data-ai",      r"\b(?:llm|machine learning|dataset|etl|data pipeline|embedding|rag|prompt|inference|analytics)\b"),
    ("fintech",      r"\b(?:invoice|payment|payroll|accounting|bookkeep|tax|bank|reconcil|billing|subscription revenue)\b"),
    ("health",       r"\b(?:patient|clinic|medical|health|ehr|therapy|doctor|nurse|prescription)\b"),
    ("education",    r"\b(?:student|teacher|classroom|course|curriculum|grading|university|tutor)\b"),
    ("ecommerce",    r"\b(?:shopify|store|inventory|sku|fulfil|shipping|order|checkout|dropship)\b"),
    ("logistics",    r"\b(?:warehouse|fleet|delivery|route|supply chain|freight|courier)\b"),
    ("hr-ops",       r"\b(?:hiring|recruit|onboarding|applicant|resume|hr team|timesheet|scheduling staff)\b"),
    ("legal",        r"\b(?:contract|compliance|legal|gdpr|lawyer|litigation|policy review)\b"),
    ("marketing",    r"\b(?:seo|campaign|ad spend|newsletter|crm|lead gen|social media manager)\b"),
    ("climate",      r"\b(?:carbon|emission|solar|energy grid|recycl|sustainab|waste)\b"),
    ("agritech",     r"\b(?:farm|crop|harvest|irrigation|livestock|agricultur)\b"),
    ("real-estate",  r"\b(?:tenant|landlord|property|lease|rent|mortgage)\b"),
    ("security",     r"\b(?:vulnerab|pentest|phishing|malware|soc2|incident response|credential)\b"),
    ("productivity", r"\b(?:calendar|meeting|note[- ]taking|todo|task manager|inbox|email triage)\b"),
]

COMPILED_DOMAINS = [(name, re.compile(rx, re.I)) for name, rx in DOMAINS]

SENTENCE = re.compile(r"(?<=[.!?])\s+")


def pain_signals(text: str) -> tuple[list[str], int]:
    """Return the labels that fired and their summed weight."""
    labels, weight = [], 0
    for w, label, rx in COMPILED:
        if rx.search(text):
            labels.append(label)
            weight += w
    return labels, weight


def problem_sentence(text: str, max_chars: int = 300) -> str:
    """The single sentence carrying the strongest pain phrase."""
    best, best_w = "", -1
    for sent in SENTENCE.split(text):
        sent = sent.strip()
        if len(sent) < 20:
            continue
        _, w = pain_signals(sent)
        if w > best_w:
            best, best_w = sent, w
    out = best or text[:max_chars]
    return out[:max_chars].strip()


def extract_who(text: str) -> str:
    m = WHO.search(text)
    if not m:
        return ""
    who = " ".join(m.group(1).split()).lower().strip(" -/")
    # "as a result", "as a matter of fact" - grammatical, not occupational
    return "" if who in {"result", "side", "matter", "rule", "bonus", "user"} else who


def guess_domain(text: str) -> str:
    best, best_n = "", 0
    for name, rx in COMPILED_DOMAINS:
        n = len(rx.findall(text))
        if n > best_n:
            best, best_n = name, n
    return best


#: A complaint ages slower than news. Three years is measured against the
#: sources: a one-year half-life scored every Stack Exchange archive row at
#: ~0.02, which is indistinguishable from deleting the source, while a problem
#: posted in 2022 is very often still a problem.
HALF_LIFE_DAYS = 1095.0


def recency_decay(posted_at: str, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Old complaints may already be solved, so weight them down - gently."""
    if not posted_at:
        return 1.0
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).days
    return 0.5 ** (max(age, 0) / half_life_days)


def impact(score: float, frequency: int = 1, severity: int = 0) -> float:
    """Ranking number that folds in the two things a lone score cannot see.

    `frequency` is how many other posts said the same thing (from `cluster`), and
    `severity` is the LLM's 1-5 read of how badly it hurts (from `extract`).
    Severity 3 is neutral, so an unjudged row is neither promoted nor punished.
    """
    freq_boost = 1.0 + math.log(max(frequency, 1))
    sev_boost = (severity or 3) / 3.0
    return round(score * freq_boost * sev_boost, 2)


def to_painpoint(post: SocialPost, min_weight: int = 2) -> PainPoint | None:
    """`None` when the post carries no pain signal worth storing."""
    body = clean(post.body)
    if len(body) < MIN_CHARS or NOISE.match(clean(post.title) or body):
        return None

    labels, weight = pain_signals(body)
    if weight < min_weight:
        return None

    # Comments count double: a thread people argue in is a pain people share.
    engagement = math.log1p(post.points + 2 * post.comments)
    score = round(weight * (1.0 + engagement) * recency_decay(post.posted_at), 2)

    return PainPoint(
        source=post.source,
        source_id=post.source_id,
        url=post.url,
        channel=post.channel,
        author=post.author,
        posted_at=post.posted_at,
        lang=post.lang,
        problem_statement=problem_sentence(body),
        raw_text=body[:4000],
        title=clean(post.title),
        who=extract_who(body),
        domain=guess_domain(body),
        signal_terms=labels,
        points=post.points,
        comments=post.comments,
        score=score,
        impact=score,          # refined later by `cluster` and `extract`
        raw=post.raw,
    )

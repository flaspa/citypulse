"""Question -> evidence clips -> grounded 30 s edit list (EDL)."""
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import llm
import settings
import timeline
from vss import VSS

Emit = Callable[[str, dict], Awaitable[None]]

FILENAME_RE = re.compile(
    r"(?P<stem>.+_(?P<set>set\d+)_video_chunk_(?P<chunk>\d+))_segment_(?P<seg>\d+)_of_(?P<total>\d+)\.mp4$")

INTERACTION_CLASSES = ["person", "bicycle", "car", "motorcycle", "bus", "truck", "traffic light", "stop sign"]

EXPAND_SYSTEM = f"""You plan searches over an archive of forward-facing dashcam drives recorded on Toronto streets.
Every 5-second clip has a factual caption written by a vision-language model (road layout, vehicles,
pedestrians, cyclists, signals, weather, what each road user is doing) plus YOLO object counts.

Given the user's question, write 3 short, concrete, visual search queries (6-12 words each) that would
match captions of clips answering it. Each query must stay squarely on the question's subject; vary
the specific behaviour or road situation, not the topic. Describe what is visible, not abstractions.

Also give the YOLO classes that must be visible for a clip to count as evidence, as groups where any
class in a group satisfies it. Choose only from: {", ".join(INTERACTION_CLASSES)}.

Return JSON only:
{{"queries": ["..."], "required_objects": [["person"], ["car", "truck", "bus"]],
  "angle": "one sentence describing the story the 30-second edit should tell"}}"""

DIRECTOR_SYSTEM = """You are the director and narrator of a 30-second documentary micro-film assembled ONLY
from dashcam footage of Toronto streets. You receive the viewer's question and a pool of candidate
clips. Each clip has a caption (what a vision model saw in it) and YOLO object counts.

1. Choose exactly 6 distinct clips that together give the strongest, most varied visual answer.
   Prefer clips whose captions clearly show the behaviour asked about. Avoid near-duplicates and
   prefer variety across drive sets.
2. Order them as an edit with an arc: establish the setting, build through specific moments,
   end on a resonant final image.
3. Write ONE continuous narration of about 70 words (hard range 65-75) in 5 sentences averaging
   14 words. It is spoken over the whole film and must read as a single coherent story with
   connective flow - not one caption per shot. Open with a sentence that frames what the shots
   share, let middle sentences move through specific moments (a sentence may draw on two clips),
   and close with a sentence that resolves the pattern the viewer has just seen.

Grounding rules (strict):
- Every concrete detail (road users, actions, colours, road features, signals, weather, numbers)
  must appear in the caption or detections of a clip that sentence cites.
- Never invent street names, neighbourhoods, landmarks, times of day, intentions, emotions or causes.
- Connective or interpretive phrasing is allowed only when it generalises what the cited clips show.
- Never mention clips, captions, cameras, footage, AI or data. Say "Toronto" at most once.
- Present tense, calm documentary voice. No questions, no exclamation marks.
- The title follows the same grounding rules.
- If the evidence note says WEAK, do not pretend the clips answer the question. Also return
  "absence_line": one plain sentence of at most 10 words saying these drives do not show what was
  asked (e.g. "These drives never meet the rain."). The narration then describes only what the
  clips actually show, in about 60 words.

Citations: write "narration" as a single string. After EVERY sentence put a bracket tag listing the
clip ids that sentence draws on, e.g. "Pedestrians stream across as traffic waits. [C4, C9]". Across
the narration all 6 chosen clips must be cited, and clips should first appear in shot order.

Return JSON only:
{"title": "3-6 word film title",
 "shots": [{"clip": "C4", "role": "establish|develop|contrast|resolve", "note": "what the viewer should notice"}],
 "narration": "First sentence. [C4, C9] Second sentence. [C9] ..."}"""

VERIFY_SYSTEM = """You are a strict fact-checker for documentary narration built from video evidence.
For each sentence, compare it with the evidence (captions + detected objects) of the clips it cites.
A detail is UNSUPPORTED if it is not stated in, or directly implied by, that evidence: invented places,
street names, landmarks, times, weather, colours, counts, intentions, emotions or causes. Generic
connective phrasing that fairly summarises the cited evidence is supported.

Return JSON only:
{"sentences": [{"index": 0, "supported": true, "unsupported": ["exact phrase", "..."]}]}"""

REWRITE_SYSTEM = """You revise documentary narration so it is fully grounded in its cited video evidence.
Keep it ONE coherent story of 65-75 words in 4-6 sentences, present tense, calm documentary voice,
with connective flow rather than one caption per shot. Remove or replace every unsupported detail
with something the cited evidence actually shows. Cite all the shot clips, and keep clips first
appearing in shot order. Never mention clips, captions, cameras, footage, AI or data.

Write "narration" as a single string with a bracket tag after EVERY sentence listing the clip ids it
draws on, e.g. "Pedestrians stream across as traffic waits. [C4, C9]".

Return JSON only: {"narration": "First sentence. [C4, C9] Second sentence. [C9] ..."}"""


@dataclass
class Candidate:
    source: str
    original_video: str
    filename: str
    drive_set: str
    chunk_index: int
    segment_number: int
    total_segments: int
    segment_start: float
    segment_end: float
    caption: str
    objects: dict
    similarity: float
    queries: list = field(default_factory=list)
    score: float = 0.0
    cid: str = ""

    @property
    def chunk_key(self) -> str:
        return f"{self.drive_set}:{self.chunk_index}"


def _parse_hit(hit: dict) -> Candidate | None:
    m = FILENAME_RE.search(hit.get("filename") or hit.get("source", "").rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        objects = json.loads(hit.get("object_counts") or "{}")
    except (TypeError, json.JSONDecodeError):
        objects = {}
    return Candidate(
        source=hit["source"],
        original_video=hit.get("original_video", ""),
        filename=hit.get("filename", ""),
        drive_set=m["set"],
        chunk_index=int(m["chunk"]),
        segment_number=int(m["seg"]),
        total_segments=int(m["total"]),
        segment_start=float(hit.get("segment_start_sec") or (int(m["seg"]) - 1) * settings.SEGMENT_SECONDS),
        segment_end=float(hit.get("segment_end_sec") or int(m["seg"]) * settings.SEGMENT_SECONDS),
        caption=(hit.get("reasoning_content") or "").strip(),
        objects=objects,
        similarity=float(hit.get("similarity_score") or 0.0),
    )


def _evidence_ok(c: Candidate, groups: list[list[str]]) -> bool:
    present = {k for k, v in c.objects.items() if v}
    return all(present.intersection(g) for g in groups if g)


def rerank(cands: dict[str, Candidate], groups: list[list[str]]) -> list[Candidate]:
    for c in cands.values():
        c.score = c.similarity + 0.025 * (len(c.queries) - 1)
        if groups:
            c.score += 0.04 if _evidence_ok(c, groups) else -0.06
    return sorted(cands.values(), key=lambda c: c.score, reverse=True)


def diverse_pool(ranked: list[Candidate], size: int = 16, per_set: int = 4, per_chunk: int = 2) -> list[Candidate]:
    pool, by_set, by_chunk = [], {}, {}
    for relax in (False, True):
        for c in ranked:
            if c in pool or len(pool) >= size:
                continue
            if not relax and (by_set.get(c.drive_set, 0) >= per_set or by_chunk.get(c.chunk_key, 0) >= per_chunk):
                continue
            pool.append(c)
            by_set[c.drive_set] = by_set.get(c.drive_set, 0) + 1
            by_chunk[c.chunk_key] = by_chunk.get(c.chunk_key, 0) + 1
    for i, c in enumerate(pool, 1):
        c.cid = f"C{i}"
    return pool


def _fmt_objects(objects: dict) -> str:
    items = sorted(((k, v) for k, v in objects.items() if v), key=lambda kv: -kv[1])
    return ", ".join(f"{k}x{v}" for k, v in items[:8]) or "none detected"


def _pool_text(pool: list[Candidate]) -> str:
    lines = []
    for c in pool:
        lines.append(
            f"{c.cid} | drive {c.drive_set}, chunk {c.chunk_index} | match {c.similarity:.2f} | "
            f"objects: {_fmt_objects(c.objects)}\n   caption: {c.caption[:900]}")
    return "\n".join(lines)


def _evidence_text(clip_ids: list[str], by_id: dict[str, Candidate]) -> str:
    return "\n".join(
        f"{cid}: objects: {_fmt_objects(by_id[cid].objects)}\n   caption: {by_id[cid].caption[:900]}"
        for cid in clip_ids if cid in by_id)


def _word_count(narration: list[dict]) -> int:
    return sum(len(s["text"].split()) for s in narration)


TAG_RE = re.compile(r"\[\s*(C\d+(?:\s*[,;/]\s*C\d+)*)\s*\]")


def normalize_narration(raw) -> list[dict]:
    """One entry per sentence; clip tags written inline (e.g. "... waits. [C3]") take precedence."""
    if isinstance(raw, str):
        items = [{"text": raw, "clips": []}]
    else:
        items = [i if isinstance(i, dict) else {"text": str(i), "clips": []} for i in raw or []]
    out = []
    for item in items:
        default = [c for c in item.get("clips") or [] if isinstance(c, str)]
        text = re.sub(r"([.!?])\s*((?:\[[^\]]*\]\s*)+)", r" \2\1 ", str(item.get("text", "")))
        for part in re.split(r"(?<=[.!?])\s+", text.strip()):
            tags = [c.strip() for m in TAG_RE.finditer(part) for c in re.split(r"[,;/]", m.group(1))]
            clean = re.sub(r"\s+", " ", TAG_RE.sub("", part)).strip()
            clean = re.sub(r"\s+([.,!?;:])", r"\1", clean)
            if clean:
                out.append({"text": clean, "clips": list(dict.fromkeys(tags or default))})
    for i, s in enumerate(out):
        if not s["clips"]:
            neighbour = next((out[j]["clips"] for j in (i - 1, i + 1) if 0 <= j < len(out) and out[j]["clips"]), [])
            s["clips"] = list(neighbour)
    return out


def normalize_plan(plan: dict) -> dict:
    shots = []
    for s in plan.get("shots") or []:
        shots.append(s if isinstance(s, dict) else {"clip": str(s)})
    return {**plan, "shots": shots, "narration": normalize_narration(plan.get("narration"))}


WORDS_MIN, WORDS_MAX = 62, 78
WEAK_EVIDENCE = 0.28


def evidence_strength(pool: list[Candidate]) -> float:
    top = sorted((c.similarity for c in pool), reverse=True)[:3]
    return sum(top) / len(top) if top else 0.0


def length_ok(plan: dict, offset: int = 0) -> bool:
    return WORDS_MIN <= _word_count(plan["narration"]) + offset <= WORDS_MAX


def validate(plan: dict, by_id: dict[str, Candidate], check_words: bool = True) -> list[str]:
    return validate_shots(plan, by_id) + validate_narration(plan, check_words)


def validate_shots(plan: dict, by_id: dict[str, Candidate]) -> list[str]:
    problems = []
    shots = [s.get("clip") for s in plan.get("shots", [])]
    if len(shots) != settings.SHOT_COUNT or len(set(shots)) != settings.SHOT_COUNT:
        problems.append(f"need exactly {settings.SHOT_COUNT} distinct shots, got {shots}")
    unknown = [s for s in shots if s not in by_id]
    if unknown:
        problems.append(f"unknown clip ids: {unknown}")
    return problems


def diversity_problems(plan: dict, by_id: dict[str, Candidate]) -> list[str]:
    problems = []
    known = [by_id[s["clip"]] for s in plan.get("shots", []) if s.get("clip") in by_id]
    chunks = [c.chunk_key for c in known]
    if len(set(chunks)) < len(chunks):
        problems.append("two shots come from the same drive/chunk; pick clips from different chunks")
    if len({c.drive_set for c in by_id.values()}) >= 3:
        per_set = {}
        for c in known:
            per_set[c.drive_set] = per_set.get(c.drive_set, 0) + 1
        crowded = [s for s, n in per_set.items() if n > 2]
        if crowded:
            problems.append(f"more than 2 shots from drive {', '.join(crowded)}; use at most 2 per drive")
    return problems


def validate_narration(plan: dict, check_words: bool = True) -> list[str]:
    problems = []
    shots = [s.get("clip") for s in plan.get("shots", [])]
    narration = plan.get("narration") or []
    if not 3 <= len(narration) <= 7:
        problems.append(f"narration must have 4-6 sentences, got {len(narration)}")
    words = _word_count(narration) if narration else 0
    if check_words and not WORDS_MIN <= words <= WORDS_MAX:
        problems.append(f"narration must be 65-75 words, got {words}")
    cited = set()
    for i, s in enumerate(narration):
        clips = s.get("clips") or []
        if not clips:
            problems.append(f"sentence {i} cites no clips")
        bad = [c for c in clips if c not in shots]
        if bad:
            problems.append(f"sentence {i} cites clips that are not among the chosen shots: {bad}")
        cited.update(clips)
    uncited = [c for c in shots if c not in cited]
    if uncited:
        problems.append(f"chosen shots never cited by the narration: {uncited}")
    return problems


def order_shots_by_narration(plan: dict) -> list[dict]:
    """Picture follows the story: shots appear in the order the narration first cites them."""
    by_clip = {s["clip"]: s for s in plan["shots"]}
    ordered, seen = [], set()
    for s in plan["narration"]:
        for c in s["clips"]:
            if c in by_clip and c not in seen:
                ordered.append(by_clip[c])
                seen.add(c)
    ordered += [s for s in plan["shots"] if s["clip"] not in seen]
    return ordered


class Director:
    def __init__(self, vss: VSS, model: str = settings.DIRECTOR_MODEL, verifier: str = settings.VERIFIER_MODEL):
        self.vss = vss
        self.model = model
        self.verifier = verifier

    async def run(self, question: str, emit: Emit | None = None) -> dict:
        emit = emit or (lambda *_: asyncio.sleep(0))
        t0 = time.time()
        timings = {}

        await emit("expanding", {"question": question})
        expansion = await llm.chat_json(EXPAND_SYSTEM, f"Question: {question}", model=self.model, temperature=0.3)
        queries = [q for q in expansion.get("queries", []) if isinstance(q, str)][:3]
        groups = [[o for o in g if o in INTERACTION_CLASSES] for g in expansion.get("required_objects", [])
                  if isinstance(g, list)]
        groups = [g for g in groups if g]
        timings["expand"] = round(time.time() - t0, 2)
        await emit("searching", {"queries": [question] + queries, "angle": expansion.get("angle", "")})

        t1 = time.time()
        all_queries = [question] + queries
        results = await asyncio.gather(*(self.vss.search(q) for q in all_queries), return_exceptions=True)
        cands: dict[str, Candidate] = {}
        errors = [f"{type(r).__name__}: {r}" for r in results if isinstance(r, Exception)]
        if len(errors) == len(results):
            raise RuntimeError("all searches failed: " + "; ".join(errors)[:600])
        for q, res in zip(all_queries, results):
            if isinstance(res, Exception):
                continue
            for hit in res.get("results", []):
                c = _parse_hit(hit)
                if not c:
                    continue
                prev = cands.get(c.source)
                if prev:
                    prev.similarity = max(prev.similarity, c.similarity)
                    prev.queries.append(q)
                else:
                    c.queries = [q]
                    cands[c.source] = c
        if not cands:
            raise RuntimeError("search returned no Toronto clips for this question")
        ranked = rerank(cands, groups)
        pool = diverse_pool(ranked)
        by_id = {c.cid: c for c in pool}
        timings["search"] = round(time.time() - t1, 2)
        await emit("candidates", {
            "count": len(cands),
            "strength": round(evidence_strength(pool), 3),
            "pool": [{"clip": c.cid, "source": c.source, "drive_set": c.drive_set, "chunk_index": c.chunk_index,
                      "segment_number": c.segment_number, "score": round(c.score, 3)} for c in pool],
            "all": [{"drive_set": c.drive_set, "chunk_index": c.chunk_index, "score": round(c.score, 3)}
                    for c in ranked],
        })

        t2 = time.time()
        await emit("directing", {"pool_size": len(pool)})
        strength = evidence_strength(pool)
        weak = strength < WEAK_EVIDENCE
        note = ("WEAK - no caption clearly shows what the viewer asked for" if weak
                else "strong - several captions match the question")
        user = (f"Viewer's question: {question}\nStory angle: {expansion.get('angle', '')}\n"
                f"Evidence note: {note} (top match strength {strength:.2f})\n\n"
                f"Candidate clips:\n{_pool_text(pool)}")
        raw = await llm.chat_json(DIRECTOR_SYSTEM, user, model=self.model, temperature=0.5)
        plan = normalize_plan(raw)
        problems = validate(plan, by_id, check_words=False)
        soft = diversity_problems(plan, by_id)
        best = (len(problems), len(soft), plan, raw)
        repairs = 0
        while (problems or soft) and repairs < 2:
            repairs += 1
            raw = await llm.chat_json(
                DIRECTOR_SYSTEM,
                f"{user}\n\nYour previous answer:\n{json.dumps(raw)}\n\nFix these problems and return the "
                f"full corrected JSON:\n- " + "\n- ".join(problems + soft),
                model=self.model, temperature=0.3)
            plan = normalize_plan(raw)
            problems = validate(plan, by_id, check_words=False)
            soft = diversity_problems(plan, by_id)
            if (len(problems), len(soft)) < best[:2]:
                best = (len(problems), len(soft), plan, raw)
        _, _, plan, raw = best
        problems = validate(plan, by_id, check_words=False)
        if problems:
            raise RuntimeError("director could not produce a valid edit: " + "; ".join(problems)
                               + f"\nlast plan: {json.dumps(plan)[:1500]}")
        framing = []
        absence = str(plan.get("absence_line") or "").strip()
        if weak and absence:
            framing = [{"text": absence, "clips": [], "framing": True}]
        offset = sum(len(s["text"].split()) for s in framing)
        for _ in range(2):
            if length_ok(plan, offset):
                break
            await self.revise(plan, by_id, [], offset)
        await emit("narration", {"title": plan.get("title", ""), "narration": framing + plan["narration"],
                                 "verified": False})
        timings["direct"] = round(time.time() - t2, 2)

        t3 = time.time()
        grounding = await self.ground(plan, by_id, offset)
        plan["narration"] = framing + plan["narration"]
        timings["verify"] = round(time.time() - t3, 2)
        await emit("narration", {"title": plan.get("title", ""), "narration": plan["narration"],
                                 "verified": grounding["verified"]})

        edl = self.build_edl(question, expansion, queries, plan, by_id, ranked, grounding)
        edl["evidence"] = {"strength": round(strength, 3), "weak": weak, "threshold": WEAK_EVIDENCE}
        timings["total"] = round(time.time() - t0, 2)
        edl["timings"] = timings
        await emit("edl", edl)
        return edl

    async def ground(self, plan: dict, by_id: dict[str, Candidate], offset: int = 0, max_rewrites: int = 2) -> dict:
        history = []
        for round_ in range(max_rewrites + 1):
            report = await self.verify(plan["narration"], by_id)
            issues = [{"index": s.get("index"), "unsupported": s.get("unsupported", [])}
                      for s in report.get("sentences", []) if not s.get("supported", True) and s.get("unsupported")]
            history.append({"round": round_, "issues": issues, "words": _word_count(plan["narration"])})
            if not issues or round_ == max_rewrites:
                return {"verified": not issues, "rounds": history, "verifier_model": self.verifier}
            await self.revise(plan, by_id, issues, offset)

    async def verify(self, narration: list[dict], by_id: dict[str, Candidate]) -> dict:
        blocks = []
        for i, s in enumerate(narration):
            blocks.append(f"Sentence {i}: {s['text']}\nCited evidence:\n{_evidence_text(s['clips'], by_id)}")
        return await llm.chat_json(VERIFY_SYSTEM, "\n\n".join(blocks), model=self.verifier, temperature=0.0)

    async def revise(self, plan: dict, by_id: dict[str, Candidate], issues: list[dict], offset: int = 0):
        """Rewrite narration in place: fix unsupported details and/or bring it to 65-75 words."""
        shots = [s["clip"] for s in plan["shots"]]
        words = _word_count(plan["narration"])
        target = 70 - offset
        asks = []
        if issues:
            asks.append(f"Unsupported details to remove or replace:\n{json.dumps(issues)}")
        if not WORDS_MIN <= words + offset <= WORDS_MAX:
            scale = target / max(words, 1)
            budgets = "\n".join(
                f"  sentence {i + 1}: currently {len(s['text'].split())} words -> rewrite in about "
                f"{max(5, round(len(s['text'].split()) * scale))} words"
                for i, s in enumerate(plan["narration"]))
            verb = "TOO LONG" if words > target else "TOO SHORT"
            asks.append(f"Length is {verb}: {words} words, target {target}. You MUST change the wording. "
                        f"Per-sentence word budgets:\n{budgets}\nKeep each sentence's clip tag.")
        current = " ".join(f"{s['text']} [{', '.join(s['clips'])}]" for s in plan["narration"])
        user = (f"Shot order: {shots}\n\nEvidence:\n{_evidence_text(shots, by_id)}\n\n"
                f"Current narration:\n{current}\n\n" + "\n\n".join(asks))
        outs = await asyncio.gather(*(llm.chat_json(REWRITE_SYSTEM, user, model=self.model, temperature=t)
                                      for t in (0.2, 0.45, 0.7)), return_exceptions=True)
        options = []
        for out in outs:
            if isinstance(out, Exception):
                continue
            candidate = {**plan, "narration": normalize_narration(out.get("narration"))}
            if not validate_narration(candidate, check_words=False):
                options.append(candidate)
        if not options:
            return
        best = min(options, key=lambda c: abs(_word_count(c["narration"]) - target))
        if issues or abs(_word_count(best["narration"]) - target) < abs(words - target):
            plan["narration"] = best["narration"]

    def build_edl(self, question, expansion, queries, plan, by_id, ranked, grounding) -> dict:
        sentences = [{"text": s["text"].strip(), "clips": s["clips"], "framing": bool(s.get("framing"))}
                     for s in plan["narration"]]
        shots = []
        for i, meta in enumerate(order_shots_by_narration(plan), 1):
            c = by_id[meta["clip"]]
            shots.append({
                "index": i,
                "clip": c.cid,
                "role": meta.get("role", ""),
                "note": meta.get("note", ""),
                "source": c.source,
                "original_video": c.original_video,
                "filename": c.filename,
                "drive_set": c.drive_set,
                "chunk_index": c.chunk_index,
                "segment_number": c.segment_number,
                "total_segments": c.total_segments,
                "segment_stem": FILENAME_RE.search(c.source)["stem"],
                "match_window": [c.segment_start, c.segment_end],
                "caption": c.caption,
                "objects": c.objects,
                "similarity": round(c.similarity, 4),
                "score": round(c.score, 4),
                "matched_queries": c.queries,
            })
        timeline.layout(shots, sentences, timeline.estimate_durations(sentences))

        selected = {(s["drive_set"], s["chunk_index"]): s["index"] for s in shots}
        return {
            "id": uuid.uuid4().hex[:12],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "question": question,
            "title": plan.get("title", "").strip(),
            "angle": expansion.get("angle", ""),
            "queries": [question] + queries,
            "required_objects": expansion.get("required_objects", []),
            "duration": settings.FILM_SECONDS,
            "narration": {
                "text": " ".join(s["text"] for s in sentences),
                "words": _word_count(sentences),
                "sentences": sentences,
                "timing_basis": "estimate",
            },
            "shots": shots,
            "sources": {
                "candidates": [{"drive_set": c.drive_set, "chunk_index": c.chunk_index, "score": round(c.score, 3),
                                "shot": selected.get((c.drive_set, c.chunk_index))} for c in ranked],
            },
            "grounding": grounding,
            "models": {"director": self.model, "verifier": self.verifier},
        }

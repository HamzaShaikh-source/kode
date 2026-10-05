"""Hackathon mode: from a problem statement to the idea the team builds.

Four rounds, every one run by the teammates' own AI agents in parallel:
  1. Research and propose: each agent studies the problem (web search when its CLI has it) and proposes up to two ideas.
  2. Critique: ideas are shown anonymously and shuffled; each agent reviews and scores the others' ideas (never its own) on five criteria.
  3. Rebuttal: each author sees the critiques of its ideas and improves them or withdraws.
  4. Vote: each agent scores the surviving ideas and casts ONE vote for an idea that is not its own.
The idea with the most votes wins; ties go to the higher mean score, then to the lower id. The agents' votes are the decision, not the lead's.
"""
import asyncio
import statistics
from typing import Any, Dict, List, Optional

from hub.judge import shuffle_seeded
from llm import extract_json

CRIT = ("impact", "feasibility", "novelty", "demo", "fit")
RUBRIC = ("impact = how much it matters to the people in the problem statement; feasibility = can this team really finish a working version in the time; "
          "novelty = how fresh, not a clone of the obvious; demo = how well it shows in a 3-minute live demo; fit = how exactly it answers the problem statement and its constraints.")

PROPOSE = """You are a teammate in a hackathon team of {n} developers, each with an AI coding agent. The whole team will build ONE project from the problem statement below.
First RESEARCH the problem: who has it, what exists already, what judges of this kind of event reward. Use web search or any research tools you have; if you cannot
search, say so in "research" and rely on what you know. Then propose {k} distinct, concrete ideas. Be specific about what the MVP does; no vague platforms.
Reply with ONLY a JSON object:
{"ideas": [{"title": "...", "pitch": "2-3 sentences: who it helps and how", "research": ["3-5 short findings, with a source or URL when you have one"],
            "stack": "languages, frameworks", "mvp": "what exists at the demo, concrete", "risks": "the main risk and how to cut it"}]}"""

CRITIQUE = """You are reviewing ideas proposed for a hackathon problem. Ideas are anonymous. {mine}Review EVERY other idea honestly: name its real strengths and its real weaknesses.
Score each from 1 to 10 on: {rubric}
Be critical and calibrated; do not give everything 8. Reply with ONLY a JSON object:
{"reviews": [{"id": "I1", "strengths": "...", "weaknesses": "...", "scores": {"impact": 7, "feasibility": 6, "novelty": 5, "demo": 8, "fit": 7}}]}"""

REBUT = """Your idea(s) were reviewed anonymously by teammates. For each of your ideas, answer the criticism and IMPROVE the idea (change the scope, fix the weakness,
sharpen the demo) or withdraw it if it cannot be saved. Reply with ONLY a JSON object:
{"revisions": [{"id": "I1", "withdraw": false, "response": "how you answer the critique", "pitch": "the improved pitch", "mvp": "the improved MVP"}]}"""

VOTE = """Final vote. You see the surviving ideas (after rebuttals) and the reviews. Score each idea from 1 to 10 overall, then cast exactly ONE vote for the idea you
think this team should build to win. {mine}Vote for the best idea for the problem statement and this team, not for the one you like most.
Reply with ONLY a JSON object: {"scores": [{"id": "I1", "score": 7}], "vote": "I2", "reason": "one or two sentences"}"""


async def _ask(client: Any, system: str, user: str, timeout: float = 600.0) -> Optional[dict]:
    try:
        res = await asyncio.wait_for(client.complete(system, user + "\n\nReply with the JSON object only.", max_tokens=4000), timeout)
        obj = extract_json(res.text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _s(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def tally(ideas: List[dict], votes: Dict[str, str], reviews: List[dict], vote_scores: Dict[str, List[float]]) -> dict:
    """Most votes wins; ties by mean score (reviews and final scores), then by lower id. Returns the ranked list."""
    live = [i for i in ideas if not i.get("withdrawn")]
    count = {i["id"]: 0 for i in live}
    for voter, iid in votes.items():
        if iid in count:
            count[iid] += 1
    allscores: Dict[str, List[float]] = {i["id"]: [] for i in live}
    for r in reviews:
        s = r.get("scores") or {}
        vals = [float(s[c]) for c in CRIT if isinstance(s.get(c), (int, float))]
        if vals and r.get("id") in allscores:
            allscores[r["id"]].append(sum(vals) / len(vals))
    for iid, vals in vote_scores.items():
        if iid in allscores:
            allscores[iid].extend(vals)
    rank = []
    for i in live:
        sc = allscores[i["id"]]
        rank.append({"id": i["id"], "title": i["title"], "votes": count[i["id"]], "score": round(statistics.mean(sc), 2) if sc else 0.0})
    rank.sort(key=lambda r: (-r["votes"], -r["score"], int(r["id"][1:])))
    return {"ranking": rank, "winner": rank[0]["id"] if rank else ""}


async def run(eng: Any, p: dict) -> None:
    """Fills p["ideation"], then returns; the caller continues with planning using the winning idea as the brief."""
    members = p["members"]
    n = len(members)
    problem = p["brief"]
    log = lambda text: eng.log(p, "ideate", text)
    clients = [(m, eng.member_client(p, m)) for m in members]
    ide: Dict[str, Any] = {"problem": problem, "rounds": [], "ideas": [], "reviews": [], "votes": [], "winner": "", "ranking": []}
    p["ideation"] = ide
    p["problem"] = problem
    p["status"], p["phase"], p["note"] = "planning", "ideate", "Hackathon mode: the agents are researching and proposing ideas..."
    eng.save(p)

    # 1 ── research and propose
    ctx = f"PROBLEM STATEMENT:\n{problem}\n\nTEAM: {n} developers with these agents: " + ", ".join(sorted({m['provider'] for m in members})) + "\n"
    k = 2 if n <= 3 else 1
    log(f"round 1 of 4: {n} agents research the problem and propose ideas")
    props = await asyncio.gather(*(_ask(c, PROPOSE.replace("{n}", str(n)).replace("{k}", str(k)), ctx) for _, c in clients))
    raw = []
    for (m, _), obj in zip(clients, props):
        for it in ((obj or {}).get("ideas") or [])[:k]:
            if isinstance(it, dict) and _s(it.get("title"), 100) and _s(it.get("pitch"), 600):
                raw.append({"author": m["name"], "node_id": m["node_id"], "title": _s(it["title"], 100), "pitch": _s(it["pitch"], 600), "research": [_s(x, 220) for x in (it.get("research") or [])][:5],
                            "stack": _s(it.get("stack"), 200), "mvp": _s(it.get("mvp"), 500), "risks": _s(it.get("risks"), 300)})
        if not obj:
            log(f"{m['name']}'s agent ({m['provider']}) could not propose ideas (no reply); continuing without it")
    if not raw:
        raise RuntimeError("no agent could propose an idea; check that Claude Code or Codex is signed in on a laptop, or choose another planner and try again")
    shuffled = shuffle_seeded(raw, p["id"])                                    # anonymous and in random order so position cannot bias the vote
    ideas = [{"id": f"I{i + 1}", **it} for i, it in enumerate(shuffled)]
    ide["ideas"] = ideas
    ide["rounds"].append({"n": 1, "title": "Research and propose", "text": f"{len(ideas)} ideas from {len({i['author'] for i in ideas})} agents"})
    log(f"{len(ideas)} ideas on the table: " + "; ".join(f"{i['id']} {i['title']}" for i in ideas))
    eng.save(p)

    def board(show_extra: bool = False) -> str:
        return "\n\n".join(f"{i['id']}: {i['title']}\nPitch: {i['pitch']}\nStack: {i['stack']}\nMVP: {i['mvp']}\nRisks: {i['risks']}\nResearch: " + " | ".join(i["research"]) for i in ideas if not i.get("withdrawn"))

    if len(ideas) == 1:
        ide["winner"] = ideas[0]["id"]
        log("only one idea was proposed: it is the winner by default")
    else:
        # 2 ── critique
        log("round 2 of 4: anonymous critique and scoring")
        revs = await asyncio.gather(*(_ask(c, CRITIQUE.replace("{rubric}", RUBRIC).replace("{mine}", "Your own ideas are " + ", ".join(i["id"] for i in ideas if i["node_id"] == m["node_id"]) + " (do not review those). " if any(i["node_id"] == m["node_id"] for i in ideas) else ""), "PROBLEM:\n" + problem + "\n\nIDEAS:\n" + board()) for m, c in clients))
        reviews: List[dict] = []
        for (m, _), obj in zip(clients, revs):
            for r in ((obj or {}).get("reviews") or []):
                if isinstance(r, dict) and any(i["id"] == r.get("id") and i["node_id"] != m["node_id"] for i in ideas):
                    reviews.append({"id": r["id"], "strengths": _s(r.get("strengths"), 400), "weaknesses": _s(r.get("weaknesses"), 400), "scores": {c: r["scores"].get(c) for c in CRIT if isinstance(r.get("scores"), dict) and isinstance(r["scores"].get(c), (int, float))}})
        ide["reviews"] = reviews
        ide["rounds"].append({"n": 2, "title": "Critique", "text": f"{len(reviews)} reviews"})
        log(f"{len(reviews)} reviews written")
        eng.save(p)
        # 3 ── rebuttal
        log("round 3 of 4: authors answer the critique and improve their ideas")

        def crit_for(iid: str) -> str:
            return "\n".join(f"- strengths: {r['strengths']} | weaknesses: {r['weaknesses']}" for r in reviews if r["id"] == iid) or "(no reviews)"
        tasks = []
        for m, c in clients:
            mine = [i for i in ideas if i["node_id"] == m["node_id"]]
            if mine:
                tasks.append((m, mine, _ask(c, REBUT, "PROBLEM:\n" + problem + "\n\nYOUR IDEAS AND THEIR REVIEWS:\n" + "\n\n".join(f"{i['id']}: {i['title']}\nPitch: {i['pitch']}\nMVP: {i['mvp']}\nREVIEWS:\n{crit_for(i['id'])}" for i in mine))))
        outs = await asyncio.gather(*(t[2] for t in tasks))
        for (m, mine, _), obj in zip(tasks, outs):
            for rv in ((obj or {}).get("revisions") or []):
                it = next((i for i in mine if i["id"] == (rv or {}).get("id")), None)
                if not it:
                    continue
                if rv.get("withdraw") is True and len([i for i in ideas if not i.get("withdrawn")]) > 2:
                    it["withdrawn"] = True
                    continue
                it["response"] = _s(rv.get("response"), 500)
                if _s(rv.get("pitch"), 600):
                    it["pitch"] = _s(rv["pitch"], 600)
                if _s(rv.get("mvp"), 500):
                    it["mvp"] = _s(rv["mvp"], 500)
        live = [i for i in ideas if not i.get("withdrawn")]
        ide["rounds"].append({"n": 3, "title": "Rebuttal", "text": f"{len(live)} ideas remain" + (f", {len(ideas) - len(live)} withdrawn" if len(live) < len(ideas) else "")})
        log(f"{len(live)} ideas remain after the rebuttal")
        eng.save(p)
        # 4 ── vote
        log("round 4 of 4: every agent votes (never for its own idea)")
        summary = "\n\n".join(f"{i['id']}: {i['title']}\nPitch: {i['pitch']}\nMVP: {i['mvp']}\nAuthor's answer to critique: {i.get('response', '-')}\nReviews:\n{crit_for(i['id'])}" for i in live)
        votes_raw = await asyncio.gather(*(_ask(c, VOTE.replace("{mine}", ("Your own ideas are " + ", ".join(i["id"] for i in live if i["node_id"] == m["node_id"]) + ": you may not vote for them. ") if any(i["node_id"] == m["node_id"] for i in live) else ""), "PROBLEM:\n" + problem + "\n\nSURVIVING IDEAS:\n" + summary) for m, c in clients))
        votes: Dict[str, str] = {}
        vscores: Dict[str, List[float]] = {}
        for (m, _), obj in zip(clients, votes_raw):
            if not obj:
                log(f"{m['name']}'s agent did not vote")
                continue
            v = str(obj.get("vote", "")).strip()
            valid = {i["id"] for i in live if i["node_id"] != m["node_id"]}
            if v not in valid:
                alt = sorted(((float(x.get("score", 0)), x.get("id")) for x in (obj.get("scores") or []) if isinstance(x, dict) and x.get("id") in valid and isinstance(x.get("score"), (int, float))), reverse=True)
                v = alt[0][1] if alt else ""
            if not v:
                log(f"{m['name']}'s vote was invalid and was ignored")
                continue
            votes[m["node_id"]] = v
            for x in (obj.get("scores") or []):
                if isinstance(x, dict) and x.get("id") in valid and isinstance(x.get("score"), (int, float)):
                    vscores.setdefault(x["id"], []).append(max(1.0, min(10.0, float(x["score"]))))
            ide["votes"].append({"voter": m["name"], "vote": v, "reason": _s(obj.get("reason"), 300)})
        result = tally(ideas, votes, ide["reviews"], vscores)
        ide["ranking"], ide["winner"] = result["ranking"], result["winner"]
        ide["rounds"].append({"n": 4, "title": "Vote", "text": f"{len(votes)} votes: " + ", ".join(f"{r['id']} {r['votes']}" for r in result["ranking"])})
        log("votes: " + ", ".join(f"{r['id']} {r['title']} ({r['votes']})" for r in result["ranking"]))
    win = next(i for i in ideas if i["id"] == ide["winner"])
    ide["winner_title"] = win["title"]
    p["brief"] = (f"{problem}\n\nCHOSEN IDEA (selected by the team's AI agents in a vote): {win['title']}\n{win['pitch']}\nMVP scope: {win['mvp']}\nSuggested stack: {win['stack']}\n"
                  f"Risk to manage: {win['risks']}\nBuild exactly this idea; keep the scope achievable, finished and demo-ready.")
    log(f"winner: {win['title']} (idea {win['id']}); planning the build around it")
    eng.save(p)

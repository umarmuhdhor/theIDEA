# Where to find winning hackathon projects

Status legend: **implemented** = there is a scraper in this repo; **verified** = the
endpoint/page was probed on 2026-09-06 and responded; **manual** = browse-only, no
clean data hook found yet.

## Tier 1 — implemented in this repo

| Platform | Winners live at | Data hook | Scale |
|---|---|---|---|
| **Devfolio** | `https://<hackathon>.devfolio.co/projects` (e.g. [onchain-summer](https://onchain-summer.devfolio.co/projects)) | `POST https://api.devfolio.co/api/search/projects` with `filter:"winners"` — returns full write-up, tech stack, team, prize + track | ~1,750 hackathons via `POST /api/search/hackathons`; Onchain Summer alone = 1,258 projects / 575 prize-winners |
| **Devpost** | `https://<hackathon>.devpost.com/project-gallery` (winner ribbon on the card) | `GET https://devpost.com/api/hackathons?status[]=ended&page=N` (JSON) + gallery HTML + `devpost.com/software/<slug>` detail page | 5,086 ended online hackathons |
| **ETHGlobal** | [ethglobal.com/showcase](https://ethglobal.com/showcase) — every prize a project won is listed on its page | No REST API; the Next.js RSC flight payload is parsed out of the HTML | Every ETHGlobal event since 2020 |

Devpost is the single biggest general-purpose pool. Devfolio is the richest per
project (it stores "the problem it solves" and "challenges we ran into" as separate
fields). ETHGlobal is the highest signal-per-project for crypto/infra ideas.

## Tier 2 — verified live, not yet implemented

| Platform | Focus | Notes |
|---|---|---|
| ~~Unstop~~ → **moved to Tier 1 (directory only)** | — | See below |
| [DoraHacks](https://dorahacks.io/hackathon) | Web3 grants + BUIDLs | Every submission is a permanent "BUIDL" page with prize info; internal API is under `/api/` but not at the path guessed here — inspect the network tab |
| [Taikai](https://taikai.network/hackathons) | EU / corporate hackathons | GraphQL backend, endpoint not at `api.taikai.network/graphql`; find it via devtools |
| [lablab.ai](https://lablab.ai/event) | AI / LLM hackathons, weekly cadence | Next.js app, winners listed per event; parse the RSC payload the same way as ETHGlobal |
| [MLH](https://mlh.io/seasons/2025/events) | Student hackathons | MLH itself only lists *events* — the projects almost always live on a Devpost subdomain, so use MLH as a seed list and hand the slugs to the Devpost scraper |
| [HackerEarth](https://www.hackerearth.com/challenges/hackathon/) | Sponsored corporate challenges | Leaderboards are public; submissions often are not |
| [Kaggle](https://www.kaggle.com/competitions) | ML competitions | Not a hackathon, but winning solution write-ups are the highest-quality "what actually worked" corpus anywhere. Official API: `pip install kaggle` |

## Unstop — implemented, but directory only

`GET https://unstop.com/api/public/opportunity/search-result?opportunity=hackathons&page=N`
lists 6,200+ hackathons with prize structures, required skills, organiser and dates.
`GET https://unstop.com/api/public/competition/<id>` gives the full event record.

**It has no public project submissions.** Sampling twelve ended hackathons found
`teams: []` and `players: []` on all twelve, `overall_leaderboard: 0`, and result
flags unset on nearly all rounds — winners and submissions sit behind a
participant/organiser login. So `UnstopScraper.hackathons()` is fully implemented and
`projects()` deliberately yields nothing rather than pretending.

Use it for discovery — it covers Indian campus and corporate events that never appear
on Devpost or Devfolio — then look the event up elsewhere for the actual projects:

```bash
./.venv/bin/python run.py list-hackathons --source unstop --limit 50
```

## Tier 3 — worth a manual sweep

- **Y Combinator / Product Hunt launches** that started as hackathon projects — good for "did the idea survive contact with a market?"
- **Encode Club**, **Gitcoin**, **Buidlbox** — Web3 hackathon organisers; results usually mirrored onto Devfolio or Devpost anyway.
- **AngelHack**, **Junction (Finland)**, **HackMIT / PennApps / TreeHacks / HackTheNorth** — the big campus events; all publish on Devpost.
- **Apify actors** — there are off-the-shelf paid scrapers for Devpost and a multi-platform hackathon aggregator if you would rather rent than build.

## Practical advice

1. Start with **Devfolio + ETHGlobal** if you want *structured* idea data — both hand you
   the problem statement and tech stack as fields, no NLP needed.
2. Use **Devpost** for breadth and for non-crypto domains (health, education, gaming,
   accessibility, RevenueCat/mobile, etc.).
3. Prize names are the label you actually want: "1st place", "Best use of X", "Finalist"
   separate real winners from participation rewards. The Devfolio scraper filters
   `Participation Reward`-style prizes out by default (`exclude_participation=True`).
4. Everything here is public, unauthenticated data. Keep the default 1 req/sec rate
   limit, keep the on-disk cache on, and check each platform's terms before running
   anything large or redistributing what you collect.

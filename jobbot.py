"""
jobbot.py - overnight job pipeline: fetch -> score -> tailor -> review queue.

Setup:
  pip install requests
  export GEMINI_API_KEY=...   (free key from aistudio.google.com)
  put your real resume as plain text in base_resume.txt
  edit CONFIG below, then run:  python jobbot.py
Schedule (runs at 2am):  0 2 * * *  cd /path && python jobbot.py

It does NOT submit forms. It prepares a tailored resume + cover letter per
good-fit job and writes queue.csv. You open the links and submit in ~20 min.
"""
import csv, json, os, re, time, pathlib
import requests

CONFIG = {
    "model": os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
    "min_score": 65,                 # only prepare jobs scoring >= this
    "max_per_run": 15,               # cap so quality stays high
    "title_keywords": ["penetration", "vapt", "security analyst", "soc",
                       "red team", "application security", "detection"],
    "location_keywords": ["india", "mumbai", "thane", "pune", "remote"],
    # Public job-board APIs. Add company slugs (from their careers page URL).
    "greenhouse": ["cloudflare", "okta"],
    "lever": ["palantir"],
    # Indian VAPT firms mostly have no public API: add their career URLs
    # to a manual list and check them yourself (see README advice).
}

OUT = pathlib.Path("out"); OUT.mkdir(exist_ok=True)
SEEN = pathlib.Path("seen.json")
seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
resume = pathlib.Path("base_resume.txt").read_text()


def get(url):
    for i in range(3):
        try:
            r = requests.get(url, timeout=45)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == 2: raise
            time.sleep(5 * (i + 1))


def fetch_greenhouse(slug):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    for j in r.json().get("jobs", []):
        yield dict(id=f"gh-{j['id']}", company=slug, title=j["title"],
                   location=j.get("location", {}).get("name", ""),
                   url=j["absolute_url"], desc=re.sub("<[^>]+>", " ", j.get("content", "")))


def fetch_lever(slug):
    r = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    for j in r.json():
        yield dict(id=f"lv-{j['id']}", company=slug, title=j["text"],
                   location=j.get("categories", {}).get("location", ""),
                   url=j["hostedUrl"], desc=j.get("descriptionPlain", ""))


def relevant(j):
    t, l = j["title"].lower(), j["location"].lower()
    hit = lambda words, text: any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in words)
    return hit(CONFIG["title_keywords"], t) and hit(CONFIG["location_keywords"], l)


def ask(prompt, system, max_tokens=1500):
    """Call Gemini free tier. Spaces calls out and retries on rate limits."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{CONFIG['model']}:generateContent")
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens + 2000}}
    for attempt in range(5):
        time.sleep(7)  # stay under free-tier requests-per-minute
        r = requests.post(url, json=body, timeout=90,
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(30 * (attempt + 1)); continue
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    raise RuntimeError("rate limited too long (daily free quota may be used up)")


def score(j):
    out = ask(f"RESUME:\n{resume}\n\nJOB: {j['title']} at {j['company']}\n{j['desc'][:6000]}\n\n"
              'Return ONLY JSON: {"score":0-100,"reason":"one blunt sentence","gaps":["..."]}. '
              "Be harsh: penalize missing required certs, years of experience, and bonds/conditions.",
              "You are a strict technical recruiter for entry-level security roles.", 400)
    return json.loads(re.search(r"\{.*\}", out, re.S).group())


RULES = ("Use ONLY facts present in the resume. Never invent skills, tools, certs, "
         "employers or numbers. Reorder and reword to match the job's language; "
         "if the job needs something the candidate lacks, leave it out rather than fake it.")


def tailor(j):
    cv = ask(f"RESUME:\n{resume}\n\nJOB: {j['title']} at {j['company']}\n{j['desc'][:6000]}\n\n"
             "Rewrite the resume tailored to this job. Plain text, ATS-clean, one page.", RULES, 2500)
    cl = ask(f"RESUME:\n{resume}\n\nJOB: {j['title']} at {j['company']}\n{j['desc'][:4000]}\n\n"
             "Write a direct 150-word cover letter. No cliches, no 'I am excited'. "
             "Lead with the most relevant real experience.", RULES, 600)
    return cv, cl


def main():
    jobs = []
    for s in CONFIG["greenhouse"]:
        try: jobs += list(fetch_greenhouse(s))
        except Exception as e: print("gh fail", s, e)
    for s in CONFIG["lever"]:
        try: jobs += list(fetch_lever(s))
        except Exception as e: print("lever fail", s, e)

    new = [j for j in jobs if j["id"] not in seen and relevant(j)]
    print(f"{len(jobs)} fetched, {len(new)} new relevant")
    rows = []
    for j in new[: CONFIG["max_per_run"]]:
        try:
            s = score(j)
            seen.add(j["id"])
            if s["score"] < CONFIG["min_score"]:
                continue
            cv, cl = tailor(j)
            d = OUT / f"{j['company']}_{j['id']}"; d.mkdir(exist_ok=True)
            (d / "resume.txt").write_text(cv); (d / "cover_letter.txt").write_text(cl)
            rows.append([s["score"], j["company"], j["title"], j["location"],
                         s["reason"], "; ".join(s["gaps"]), j["url"], str(d)])
        except Exception as e:
            print("skip", j["title"], e)
        time.sleep(1)

    SEEN.write_text(json.dumps(sorted(seen)))
    rows.sort(reverse=True)
    new_file = not pathlib.Path("queue.csv").exists()
    with open("queue.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["score", "company", "title", "location", "reason", "gaps", "apply_url", "folder"])
        w.writerows(rows)
    print(f"{len(rows)} jobs ready in queue.csv")


if __name__ == "__main__":
    main()

"""
JobHunter Agent - Backend API
Powered by TinyFish Web Agent
"""

import asyncio
import json
import os
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="JobHunter Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TINYFISH_API_KEY = os.environ.get("TINYFISH_API_KEY", "")
TINYFISH_BASE_URL = "https://agent.tinyfish.ai/v1/automation"


def build_goal(job_title: str, location: str, site: str) -> str:
    """Build a precise, structured goal prompt for TinyFish."""
    return f"""
You are a job data extraction agent. Your task:

1. Search for "{job_title}" jobs in "{location}" on this job site.
2. Navigate the search results page.
3. Extract data for the first 5 job listings you find.

For EACH job listing, extract ALL of the following fields:
- title: exact job title
- company: company name
- location: job location (city/remote/hybrid)
- salary: salary range if shown, otherwise "Not disclosed"
- experience: years of experience required if mentioned, otherwise "Not specified"
- skills: list up to 5 key skills or technologies mentioned
- posted: when the job was posted (e.g., "2 days ago", "Today")
- apply_url: the full URL to apply or view the job

Return ONLY a valid JSON object with this exact structure:
{{
  "site": "{site}",
  "jobs": [
    {{
      "title": "...",
      "company": "...",
      "location": "...",
      "salary": "...",
      "experience": "...",
      "skills": ["...", "..."],
      "posted": "...",
      "apply_url": "..."
    }}
  ]
}}

Do not include any explanation or text outside the JSON.
""".strip()


SOURCES = {
    "Indeed": "https://www.indeed.com/jobs?q={job_title}&l={location}",
    "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords={job_title}&location={location}",
    "Glassdoor": "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={job_title}&locT=C&locId=1&locKeyword={location}",
}


async def stream_tinyfish(url: str, goal: str, site_name: str) -> AsyncGenerator[str, None]:
    """Stream a single TinyFish run and yield SSE-formatted events."""
    headers = {
        "X-API-Key": TINYFISH_API_KEY,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "url": url,
        "goal": goal,
        "browser_profile": "stealth",  # stealth for job sites (anti-bot)
    }

    yield f"data: {json.dumps({'type': 'AGENT_START', 'site': site_name, 'url': url})}\n\n"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{TINYFISH_BASE_URL}/run-sse",
                                     headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    yield f"data: {json.dumps({'type': 'ERROR', 'site': site_name, 'message': f'HTTP {response.status_code}: {body.decode()}'})}\n\n"
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                        etype = event.get("type", "")

                        if etype == "PROGRESS":
                            yield f"data: {json.dumps({'type': 'PROGRESS', 'site': site_name, 'message': event.get('purpose', '')})}\n\n"

                        elif etype == "STREAMING_URL":
                            yield f"data: {json.dumps({'type': 'STREAMING_URL', 'site': site_name, 'streaming_url': event.get('streaming_url', '')})}\n\n"

                        elif etype == "COMPLETE":
                            status = event.get("status", "FAILED")
                            result = event.get("result", {})
                            yield f"data: {json.dumps({'type': 'SITE_COMPLETE', 'site': site_name, 'status': status, 'result': result})}\n\n"

                        elif etype in ("FAILED", "ERROR"):
                            yield f"data: {json.dumps({'type': 'ERROR', 'site': site_name, 'message': event.get('error', {}).get('message', 'Unknown error')})}\n\n"

                    except json.JSONDecodeError:
                        continue

    except Exception as e:
        yield f"data: {json.dumps({'type': 'ERROR', 'site': site_name, 'message': str(e)})}\n\n"


@app.get("/search")
async def search_jobs(
    job_title: str = Query(..., description="Job title to search for"),
    location: str = Query(..., description="Location to search in"),
    sites: str = Query("Indeed,LinkedIn", description="Comma-separated list of sites"),
):
    """
    Stream job search results from multiple sites in parallel.
    Returns Server-Sent Events (SSE).
    """
    selected_sites = [s.strip() for s in sites.split(",") if s.strip() in SOURCES]
    if not selected_sites:
        selected_sites = ["Indeed"]

    async def event_generator() -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'type': 'SEARCH_START', 'job_title': job_title, 'location': location, 'sites': selected_sites})}\n\n"

        # Run all selected sites concurrently
        tasks = []
        for site_name in selected_sites:
            url_template = SOURCES[site_name]
            url = url_template.format(
                job_title=job_title.replace(" ", "+"),
                location=location.replace(" ", "+"),
            )
            goal = build_goal(job_title, location, site_name)
            tasks.append(stream_tinyfish(url, goal, site_name))

        # Merge all SSE streams
        async def collect(gen, queue):
            async for item in gen:
                await queue.put(item)
            await queue.put(None)  # sentinel

        queue = asyncio.Queue()
        runners = [asyncio.create_task(collect(t, queue)) for t in tasks]

        finished = 0
        while finished < len(tasks):
            item = await queue.get()
            if item is None:
                finished += 1
            else:
                yield item

        yield f"data: {json.dumps({'type': 'ALL_DONE'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "api_key_set": bool(TINYFISH_API_KEY)}

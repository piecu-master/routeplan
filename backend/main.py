import os
import httpx
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="RoutePlan API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OSRM_URL = os.environ.get("OSRM_URL", "http://router.project-osrm.org")
NOMINATIM_URL = "https://nominatim.openstreetmap.org"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = "gpt-4o-mini"
USE_LLM_RERANKER = os.environ.get("USE_LLM_RERANKER", "false").lower() == "true"


class RouteRequest(BaseModel):
    origin: str
    destination: str
    depart_at: str | None = None  # ISO datetime, optional
    tolerance_hours: int = 0  # +/- hours to search (0-8)
    granularity_min: int = 15  # minutes between candidate departures


class CandidateInfo(BaseModel):
    depart_iso: str
    score: float
    reason: str


class RouteResponse(BaseModel):
    origin: str
    destination: str
    recommended_depart_at: str
    duration_minutes: int
    distance_km: float
    weather_summary: str
    traffic_summary: str
    advice: str
    candidates: List[CandidateInfo] = []
    safety_check: str = "ok"


@app.get("/health")
def health():
    return {"status": "ok"}


async def geocode(location: str) -> tuple[float, float]:
    """Geocode location name to coordinates using Nominatim."""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{NOMINATIM_URL}/search",
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": "RoutePlan/1.0"},
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        if not data:
            raise HTTPException(status_code=400, detail=f"Location not found: {location}")
        return float(data[0]["lat"]), float(data[0]["lon"])


async def get_route(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> dict:
    """Get route from OSRM."""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{OSRM_URL}/route/v1/driving/{origin_lon},{origin_lat};{dest_lon},{dest_lat}",
            params={"overview": "full", "steps": "true", "geometries": "geojson"},
            timeout=10
        )
        res.raise_for_status()
        return res.json()


def _is_good_weather_code(code: int) -> bool:
    """Return True for non-precipitating/clear-ish weather codes.
    This is a conservative heuristic: treat only codes 0-3 as 'good'.
    """
    try:
        return int(code) in (0, 1, 2, 3)
    except Exception:
        return False


def _weather_code_description(code: int) -> str:
    """Human-readable description of weather code."""
    if code in (0, 1):
        return "clear/sunny"
    elif code in (2, 3):
        return "cloudy"
    elif code in (51, 53, 55):
        return "light drizzle"
    elif code in (61, 63, 65):
        return "rain"
    elif code in (71, 73, 75):
        return "snow"
    elif code in (80, 81, 82):
        return "rain showers"
    elif code in (95, 96, 99):
        return "thunderstorm/severe"
    else:
        return "fog/unknown"


async def _get_weather_code_at(lat: float, lon: float, when: datetime) -> int:
    """Query Open-Meteo hourly weathercode and return nearest-hour code."""
    date_str = when.date().isoformat()
    async with httpx.AsyncClient() as client:
        res = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "weathercode",
                "timezone": "UTC",
                "start_date": date_str,
                "end_date": date_str,
            },
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
        times = data.get("hourly", {}).get("time", [])
        codes = data.get("hourly", {}).get("weathercode", [])
        if not times or not codes:
            # fallback: treat as unknown -> bad
            return -1

        # find nearest hour
        from bisect import bisect_left

        # times are ISO strings in UTC
        time_objs = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in times]
        target = when.replace(tzinfo=timezone.utc)
        idx = bisect_left(time_objs, target)
        if idx == 0:
            return int(codes[0])
        if idx >= len(time_objs):
            return int(codes[-1])
        # pick closer of idx-1 and idx
        before = time_objs[idx - 1]
        after = time_objs[idx]
        pick = idx - 1 if (target - before) <= (after - target) else idx
        return int(codes[pick])


async def get_weather(lat: float, lon: float) -> str:
    """Get weather summary from Open-Meteo (no API key required)."""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m"
            },
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        current = data.get("current", {})
        temp = current.get("temperature_2m", "N/A")
        wind = current.get("wind_speed_10m", "N/A")
        return f"{temp}°C, wind {wind}km/h"


async def rerank_with_llm(candidates_data: List, route_info: dict) -> Optional[dict]:
    """Call LLM to rerank and score candidates with explainability & safety.
    Returns {best_idx, candidates_scores} or None on failure.
    """
    if not USE_LLM_RERANKER or not LLM_API_KEY:
        return None

    candidates_json = []
    for i, cand in enumerate(candidates_data):
        samples_desc = []
        for j, code in enumerate(cand["codes"]):
            arrival = cand["arrivals"][j]
            samples_desc.append({
                "point": j,
                "weather": _weather_code_description(code),
                "weather_code": code,
                "arrival_iso": arrival.isoformat(),
            })
        candidates_json.append({
            "id": i,
            "depart_iso": cand["depart"].isoformat(),
            "samples": samples_desc,
        })

    prompt = f"""Score these route departure candidates 0.0-1.0 (best=1.0, worst=0.0).

Route: {route_info['origin']} → {route_info['destination']}
Duration: {route_info['duration_min']} min

RUBRIC:
- Severe (codes 95-99): 0.0
- Snow (71-77): 0.1-0.3
- Heavy rain (65): 0.2-0.4
- Light rain (61-63): 0.4-0.6
- Drizzle (51-55): 0.6-0.7
- Cloudy (2-3): 0.7-0.8
- Clear (0-1): 0.9-1.0

SAFETY RULE: Never score >0.7 if ANY checkpoint has severe weather (95-99).

Candidates:
{json.dumps(candidates_json, indent=2)}

Return JSON only:
{{
  "candidates": [{{"id": 0, "score": 0.75, "reason": "reason"}}, ...],
  "best_id": 0,
  "safety_notes": "any warnings or 'none'"
}}"""

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 1000,
                },
                timeout=30,
            )
            res.raise_for_status()
            data = res.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            return result
    except Exception as e:
        print(f"LLM reranker error: {e}")
        return None


@app.post("/route", response_model=RouteResponse)
async def plan_route(req: RouteRequest):
    try:
        # Geocode origin and destination
        origin_lat, origin_lon = await geocode(req.origin)
        dest_lat, dest_lon = await geocode(req.destination)

        # Get route from OSRM (route geometry + duration)
        route_data = await get_route(origin_lat, origin_lon, dest_lat, dest_lon)
        if not route_data.get("routes"):
            raise HTTPException(status_code=400, detail="No route found")

        route = route_data["routes"][0]
        distance_km = route["distance"] / 1000
        duration_seconds = float(route["duration"])
        duration_minutes = int(duration_seconds / 60)

        # Extract coordinates from geojson geometry (lon, lat)
        coords = []
        geom = route.get("geometry")
        if geom and isinstance(geom, dict) and geom.get("coordinates"):
            coords = geom.get("coordinates")

        # Sampling points along route (fall back to origin/dest if geometry missing)
        sample_count = 6
        sample_points = []
        if coords:
            L = len(coords)
            if L == 1:
                sample_points = [(coords[0][1], coords[0][0])]
            else:
                for i in range(sample_count):
                    idx = int(round(i * (L - 1) / (sample_count - 1)))
                    lon, lat = coords[idx]
                    sample_points.append((lat, lon))
        else:
            sample_points = [(origin_lat, origin_lon), (dest_lat, dest_lon)]

        # Candidate departure times
        if req.depart_at:
            try:
                desired = datetime.fromisoformat(req.depart_at)
                if desired.tzinfo is None:
                    desired = desired.replace(tzinfo=timezone.utc)
            except Exception:
                desired = datetime.now(timezone.utc)
        else:
            desired = datetime.now(timezone.utc)

        tol = max(0, min(8, int(req.tolerance_hours or 0)))
        gran_min = max(1, int(req.granularity_min or 15))
        step = timedelta(minutes=gran_min)
        start = desired - timedelta(hours=tol)
        end = desired + timedelta(hours=tol)

        candidates = []
        t = start
        while t <= end:
            candidates.append(t)
            t += step

        if not candidates:
            candidates = [desired]

        best = None
        best_score = -1.0
        best_details = None
        all_candidates = []

        # evaluate candidates
        for cand in candidates:
            good = 0
            total = len(sample_points)
            cand_codes = []
            cand_arrivals = []
            # assume linear time distribution along route
            for i, (plat, plon) in enumerate(sample_points):
                frac = 0.0 if total == 1 else (i / (total - 1))
                arrival = cand + timedelta(seconds=duration_seconds * frac)
                code = await _get_weather_code_at(plat, plon, arrival)
                cand_codes.append(code)
                cand_arrivals.append(arrival)
                if _is_good_weather_code(code):
                    good += 1

            score = good / total if total > 0 else 0.0
            all_candidates.append({
                "depart": cand,
                "codes": cand_codes,
                "arrivals": cand_arrivals,
                "det_score": score
            })
            if score > best_score:
                best_score = score
                best = cand
                best_details = {"good": good, "total": total}

        # Try LLM reranking if enabled
        llm_result = await rerank_with_llm(
            all_candidates,
            {
                "origin": req.origin,
                "destination": req.destination,
                "duration_min": duration_minutes
            }
        )

        safety_check = "ok"
        candidates_info = []
        if llm_result:
            # Use LLM scores
            try:
                best_idx = llm_result.get("best_id", 0)
                if 0 <= best_idx < len(all_candidates):
                    best = all_candidates[best_idx]["depart"]
                    best_score = next((c["score"] for c in llm_result.get("candidates", []) if c.get("id") == best_idx), best_score)
                    best_details = {"good": best_details.get("good", 0), "total": best_details.get("total", 1)}
                
                safety_notes = llm_result.get("safety_notes", "")
                if safety_notes and safety_notes.lower() != "none":
                    safety_check = f"warning: {safety_notes}"
                
                # Build candidate info for response
                for cand_rec in llm_result.get("candidates", []):
                    cand_idx = cand_rec.get("id", 0)
                    if 0 <= cand_idx < len(all_candidates):
                        candidates_info.append(CandidateInfo(
                            depart_iso=all_candidates[cand_idx]["depart"].isoformat(),
                            score=cand_rec.get("score", 0.0),
                            reason=cand_rec.get("reason", "")
                        ))
            except Exception as e:
                print(f"Failed to use LLM result: {e}")
                safety_check = "llm_failed"
        else:
            # Fallback: use deterministic scores
            for i, cand in enumerate(all_candidates):
                candidates_info.append(CandidateInfo(
                    depart_iso=cand["depart"].isoformat(),
                    score=cand["det_score"],
                    reason=f"{int(cand['det_score']*100)}% good checkpoints"
                ))

        recommended_depart_at = best.isoformat() if best is not None else desired.isoformat()
        weather_summary = f"{int(best_score*100)}% of sampled points good" if llm_result is None else f"LLM scored {int(best_score*100)}/100"

        return RouteResponse(
            origin=req.origin,
            destination=req.destination,
            recommended_depart_at=recommended_depart_at,
            duration_minutes=duration_minutes,
            distance_km=round(distance_km, 2),
            weather_summary=weather_summary,
            traffic_summary="OSRM routing (no live traffic)",
            advice=f"Recommended depart: {recommended_depart_at} (maximizes clear conditions)",
            candidates=candidates_info,
            safety_check=safety_check
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Route service error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

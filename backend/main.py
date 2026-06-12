import os
import httpx
import asyncio
import time
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional

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
WEATHER_CACHE: dict = {}
CACHE_TTL = 60 * 60  # 1 hour


class RouteRequest(BaseModel):
    origin: str
    destination: str
    depart_at: str | None = None  # ISO datetime, optional
    tolerance_hours: int = 0  # +/- hours to search (0-8)
    granularity_min: int = 15  # minutes between candidate departures


class RouteResponse(BaseModel):
    origin: str
    destination: str
    recommended_depart_at: str
    duration_minutes: int
    distance_km: float
    weather_summary: str
    traffic_summary: str
    advice: str
    route_geometry: Optional[Dict] = None
    samples: List[Dict] = []


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


async def _get_weather_code_at(lat: float, lon: float, when: datetime) -> int:
    """Query Open-Meteo hourly weathercode and return nearest-hour code."""
    date_str = when.date().isoformat()
    cache_key = f"{lat:.4f},{lon:.4f},{date_str}"
    now_ts = time.time()
    # return cached if fresh
    cached = WEATHER_CACHE.get(cache_key)
    if cached and (now_ts - cached[0]) < CACHE_TTL:
        data = cached[1]
    else:
        # retry on 429 with exponential backoff
        backoff = 1
        last_exc = None
        for attempt in range(4):
            try:
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
                    if res.status_code == 429:
                        last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=res.request, response=res)
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    res.raise_for_status()
                    data = res.json()
                    # cache successful response
                    WEATHER_CACHE[cache_key] = (time.time(), data)
                    last_exc = None
                    break
            except httpx.HTTPStatusError as e:
                last_exc = e
                await asyncio.sleep(backoff)
                backoff *= 2
            except Exception as e:
                last_exc = e
                await asyncio.sleep(backoff)
                backoff *= 2
        if last_exc is not None:
            raise last_exc
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

        # evaluate candidates
        for cand in candidates:
            good = 0
            total = len(sample_points)
            sample_codes = []
            # assume linear time distribution along route
            for i, (plat, plon) in enumerate(sample_points):
                frac = 0.0 if total == 1 else (i / (total - 1))
                arrival = cand + timedelta(seconds=duration_seconds * frac)
                code = await _get_weather_code_at(plat, plon, arrival)
                sample_codes.append({"lat": plat, "lon": plon, "weather_code": code, "arrival": arrival.isoformat()})
                if _is_good_weather_code(code):
                    good += 1

            score = good / total if total > 0 else 0.0
            if score > best_score:
                best_score = score
                best = cand
                best_details = {"good": good, "total": total}
                best_sample_codes = sample_codes

        recommended_depart_at = best.isoformat() if best is not None else desired.isoformat()

        weather_summary = f"{int(best_score*100)}% of sampled points good ({best_details['good']}/{best_details['total']})"

        return RouteResponse(
            origin=req.origin,
            destination=req.destination,
            recommended_depart_at=recommended_depart_at,
            duration_minutes=duration_minutes,
            distance_km=round(distance_km, 2),
            weather_summary=weather_summary,
            traffic_summary="OSRM routing (no live traffic)",
            advice=f"Recommended depart: {recommended_depart_at} (maximizes clear conditions)",
            route_geometry=geom,
            samples=best_sample_codes if 'best_sample_codes' in locals() else []
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Route service error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

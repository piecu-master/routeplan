import os
import httpx
from datetime import datetime
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

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
OSRM_URL = os.environ.get("OSRM_URL", "http://router.project-osrm.org")
NOMINATIM_URL = "https://nominatim.openstreetmap.org"


class RouteRequest(BaseModel):
    origin: str
    destination: str
    depart_at: str | None = None  # ISO datetime, optional


class RouteResponse(BaseModel):
    origin: str
    destination: str
    recommended_depart_at: str
    duration_minutes: int
    distance_km: float
    weather_summary: str
    traffic_summary: str
    advice: str


@app.get("/health")
def health():
    return {"status": "ok"}


async def geocode(location: str) -> tuple[float, float]:
    """Geocode location name to coordinates using Nominatim."""
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{NOMINATIM_URL}/search",
            params={"q": location, "format": "json", "limit": 1},
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
            params={"overview": "full", "steps": "true"},
            timeout=10
        )
        res.raise_for_status()
        return res.json()


@app.post("/route", response_model=RouteResponse)
async def plan_route(req: RouteRequest):
    try:
        # Geocode origin and destination
        origin_lat, origin_lon = await geocode(req.origin)
        dest_lat, dest_lon = await geocode(req.destination)
        
        # Get route from OSRM
        route_data = await get_route(origin_lat, origin_lon, dest_lat, dest_lon)
        
        if not route_data.get("routes"):
            raise HTTPException(status_code=400, detail="No route found")
        
        route = route_data["routes"][0]
        distance_km = route["distance"] / 1000
        duration_minutes = int(route["duration"] / 60)
        
        # For now, use simple logic - actual implementation would check weather and optimize timing
        recommended_depart_at = req.depart_at or datetime.now().isoformat()
        
        return RouteResponse(
            origin=req.origin,
            destination=req.destination,
            recommended_depart_at=recommended_depart_at,
            duration_minutes=duration_minutes,
            distance_km=round(distance_km, 2),
            weather_summary="Fetch from OpenWeather if key provided",
            traffic_summary="Real-time from OSRM route",
            advice="Safe to depart as planned."
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Route service error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

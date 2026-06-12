import os
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
GOOGLEMAPS_API_KEY = os.environ.get("GOOGLEMAPS_API_KEY", "")


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


@app.post("/route", response_model=RouteResponse)
async def plan_route(req: RouteRequest):
    # TODO: call Google Maps Directions API for route + traffic
    # TODO: call OpenWeather API for weather at origin/destination
    # TODO: run best-time logic across a time window
    raise HTTPException(status_code=501, detail="Route planning not implemented yet.")

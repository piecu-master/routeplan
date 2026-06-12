import { useState, useRef, useEffect } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

const API = "/api";

export default function App() {
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [departAt, setDepartAt] = useState("");
  const [toleranceHours, setToleranceHours] = useState(2);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const mapRef = useRef(null);
  const mapInstanceRef = useRef(null);

  // Initialize map
  useEffect(() => {
    if (!mapRef.current || mapInstanceRef.current) return;
    
    const map = L.map(mapRef.current).setView([51.9194, 19.1451], 6); // Center on Poland
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '© OpenStreetMap contributors'
    }).addTo(map);
    
    mapInstanceRef.current = map;
  }, []);

  const plan = async () => {
    if (!origin.trim() || !destination.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const res = await fetch(`${API}/route`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ origin, destination, depart_at: departAt || null, tolerance_hours: parseInt(toleranceHours, 10) || 0, granularity_min: 15 }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={s.shell}>
      <header style={s.header}>
        <span style={s.logo}>RoutePlan</span>
        <span style={s.tagline}>Smart travel timing</span>
      </header>

      <main style={s.main}>
        <section style={s.card}>
          <p style={s.label}>PLAN YOUR ROUTE</p>

          <div style={s.field}>
            <label style={s.fieldLabel}>Origin</label>
            <input
              style={s.input}
              placeholder="e.g. Warsaw, Poland"
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
            />
          </div>

          <div style={s.field}>
            <label style={s.fieldLabel}>Destination</label>
            <input
              style={s.input}
              placeholder="e.g. Kraków, Poland"
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
            />
          </div>

          <div style={s.field}>
            <label style={s.fieldLabel}>Depart at (optional)</label>
            <input
              style={s.input}
              type="datetime-local"
              value={departAt}
              onChange={(e) => setDepartAt(e.target.value)}
            />
          </div>

          <div style={s.field}>
            <label style={s.fieldLabel}>Tolerance ± hours</label>
            <select style={s.input} value={toleranceHours} onChange={(e) => setToleranceHours(e.target.value)}>
              {[0,1,2,3,4,5,6,7,8].map((h) => (
                <option key={h} value={h}>{`± ${h} h`}</option>
              ))}
            </select>
          </div>

          <button style={s.btn} onClick={plan} disabled={loading || !origin || !destination}>
            {loading ? "Analysing…" : "Find best time"}
          </button>

          {error && <p style={s.error}>{error}</p>}
        </section>

        <div style={s.mapContainer} ref={mapRef}></div>

        {result && (
          <section style={s.card}>
            <p style={s.label}>RECOMMENDATION</p>
            <div style={s.resultGrid}>
              <Stat label="Depart at" value={result.recommended_depart_at} />
              <Stat label="Duration" value={`${result.duration_minutes} min`} />
              <Stat label="Distance" value={`${result.distance_km} km`} />
              <Stat label="Weather" value={result.weather_summary} />
              <Stat label="Traffic" value={result.traffic_summary} />
            </div>
            <p style={s.advice}>{result.advice}</p>
            
            {result.safety_check !== "ok" && (
              <div style={s.safetyWarning}>
                ⚠️ {result.safety_check}
              </div>
            )}

            {result.candidates && result.candidates.length > 0 && (
              <div style={s.candidatesSection}>
                <p style={s.label}>ALTERNATIVE TIMES (ranked)</p>
                <div style={s.candidatesList}>
                  {result.candidates.slice(0, 5).map((cand, i) => (
                    <div key={i} style={s.candidateItem}>
                      <div style={s.candidateHeader}>
                        <span style={s.candidateTime}>{new Date(cand.depart_iso).toLocaleTimeString()}</span>
                        <span style={s.candidateScore}>{(cand.score * 100).toFixed(0)}%</span>
                      </div>
                      <span style={s.candidateReason}>{cand.reason}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div style={s.stat}>
      <span style={s.statLabel}>{label}</span>
      <span style={s.statValue}>{value}</span>
    </div>
  );
}

const s = {
  shell: { maxWidth: 680, margin: "0 auto", padding: "0 16px", minHeight: "100vh" },
  header: { display: "flex", alignItems: "baseline", gap: 12, padding: "24px 0 20px", borderBottom: "1px solid var(--border)" },
  logo: { fontFamily: "var(--mono)", fontWeight: 600, fontSize: 18, color: "var(--text)" },
  tagline: { fontSize: 12, fontFamily: "var(--mono)", color: "var(--muted)" },
  main: { display: "flex", flexDirection: "column", gap: 16, padding: "20px 0" },
  card: { background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 20 },
  mapContainer: { height: 400, borderRadius: 10, border: "1px solid var(--border)", marginBottom: 16 },
  label: { fontFamily: "var(--mono)", fontSize: 10, color: "var(--muted)", letterSpacing: "0.1em", marginBottom: 16 },
  field: { marginBottom: 12 },
  fieldLabel: { display: "block", fontSize: 12, color: "var(--muted)", marginBottom: 6, fontFamily: "var(--mono)" },
  input: { width: "100%", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text)", fontSize: 14, padding: "9px 12px", outline: "none" },
  btn: { marginTop: 8, width: "100%", background: "var(--accent)", border: "none", borderRadius: 8, color: "#fff", fontSize: 14, fontWeight: 500, padding: "11px 0" },
  error: { marginTop: 12, color: "var(--error)", fontSize: 13, fontFamily: "var(--mono)" },
  resultGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 },
  stat: { background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 14px" },
  statLabel: { display: "block", fontSize: 10, fontFamily: "var(--mono)", color: "var(--muted)", marginBottom: 4 },
  statValue: { fontSize: 14, color: "var(--text)" },
  advice: { fontSize: 14, color: "var(--text)", lineHeight: 1.7, borderTop: "1px solid var(--border)", paddingTop: 14 },
  safetyWarning: { marginTop: 16, padding: 12, background: "var(--bg)", border: "1px solid var(--error)", borderRadius: 8, color: "var(--error)", fontSize: 12, fontFamily: "var(--mono)" },
  candidatesSection: { marginTop: 20, borderTop: "1px solid var(--border)", paddingTop: 20 },
  candidatesList: { display: "flex", flexDirection: "column", gap: 8 },
  candidateItem: { background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, padding: 12 },
  candidateHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  candidateTime: { fontFamily: "var(--mono)", fontSize: 13, fontWeight: 500, color: "var(--text)" },
  candidateScore: { fontSize: 12, fontWeight: 600, color: "var(--accent)" },
  candidateReason: { fontSize: 12, color: "var(--muted)", fontStyle: "italic" },
};

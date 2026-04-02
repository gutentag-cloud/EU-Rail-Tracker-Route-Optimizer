# 🚆 EU Rail Tracker & Route Optimizer (Developing)

A real-time European train tracker and route optimization platform built with open data and open-source tools. Track live trains on a map, find optimal routes using graph algorithms, and analyze delays across the EU rail network.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110-green)
![License](https://img.shields.io/badge/license-MIT-orange)

---

## ✨ Features

| Feature | Description |
|---|---|
| **🔴 Live Train Tracking** | Interpolated GPS positions of trains in real-time |
| **🗺️ Route Optimization** | Dijkstra & A* algorithms with haversine heuristic |
| **🚄 Multi-Operator** | DB, ÖBB, SBB, SNCF via HAFAS protocol |
| **⏱️ Timetable Graph** | Time-expanded routing with exact departure times |
| **📊 Pareto Optimization** | Multi-criteria routes (time vs transfers vs distance) |
| **🟢🔴 Delay Heatmap** | Live delay aggregation colored on the map |
| **🛤️ Track Geometry** | Real rail polylines from OpenStreetMap/Overpass |
| **📱 PWA** | Installable mobile app with offline support |
| **⚡ WebSockets** | Real-time push updates (no polling) |
| **🗄️ PostgreSQL + PostGIS** | Spatial queries for nearby stations |
| **🚀 Redis Cache** | Sub-second API responses with smart caching |
| **♿ Auto-Refresh** | Trains move on the map every 15 seconds |

---

## 🏗️ Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                          FRONTEND                           │
│     Leaflet Map · WebSocket Client · PWA Service Worker     │
└────────────────┬──────────────────────┬─────────────────────┘
                 │ HTTP/REST            │ WebSocket
┌────────────────▼──────────────────────▼─────────────────────┐
│                      FastAPI Backend                        │
│ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───────────┐       │
│ │API Client│ │ Optimizer │ │Delay Track│ │ Overpass │       │
│ │(HAFAS)   │ │(Graph Alg)│ │(Heatmap) │ │(Geometry) │       │
│ └────┬─────┘ └─────┬─────┘ └─────┬────┘ └─────┬─────┘       │
│      │             │             │            │             │
│ ┌────▼──────────────▼──────────────▼──────────────▼──────┐  │
│ │                   Redis Cache Layer                    │  │
│ └────────────────────────┬────────────────────────────────┘  │
│                          │                                  │
│ ┌────────────────────────▼────────────────────────────────┐  │
│ │               PostgreSQL + PostGIS (optional)           │  │
│ └─────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
       │                    │                    │
┌────▼────┐          ┌─────▼─────┐         ┌────▼────────┐
│ DB API  │          │  ÖBB API  │         │ Overpass API│
│transport│          │   HAFAS   │         │    (OSM)    │
│  .rest  │          │   mgate   │         │             │
└─────────┘          └───────────┘         └─────────────┘
```

---

## 📋 Prerequisites

- **Python 3.11+**
- **Docker & Docker Compose** (for PostgreSQL + Redis)
- **~50 MB disk** for station data

---

## 🚀 Quick Start

### Option A: Minimal (no Docker)
Works without PostgreSQL/Redis — uses in-memory fallbacks.

```bash
# Clone
git clone [https://github.com/gutentag-cloud/EU-Rail-Tracker-Route-Optimizer](https://github.com/gutentag-cloud/EU-Rail-Tracker-Route-Optimizer.git)
cd eu-rail-tracker

# Setup
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Download station data
python scripts/download_data.py

# Run
make dev
# → Open http://localhost:8000
```

### Option B: Full Stack (Docker)

```bash
# Clone
git clone [https://github.com/gutentag-cloud/EU-Rail-Tracker-Route-Optimizer.git]
cd eu-rail-tracker

# Copy env config
cp .env.example .env

# Start everything
docker-compose up -d

# Download data & run migrations
docker-compose exec app python scripts/download_data.py
docker-compose exec db psql -U rail -d railtracker -f /migrations/001_initial.sql

# App is at http://localhost:8000
```

---

## 🔌 API Reference

### Stations

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/stations/search?q=berlin&country=DE` | Search by name |
| GET | `/api/stations/nearby?lat=52.5&lon=13.4&radius=50` | Nearby stations |
| GET | `/api/stations/main?country=DE` | Main stations |

### Live Tracking

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/departures/{stop_id}` | Station departures |
| GET | `/api/trains/live/{stop_id}` | Live train positions |
| GET | `/api/trip/{trip_id}` | Full trip details |
| WS | `/ws/trains/{stop_id}` | WebSocket live stream |

### Route Optimization

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/route/optimize?from_id=X&to_id=Y&algorithm=astar` | Optimal route |
| GET | `/api/route/pareto?from_id=X&to_id=Y` | Pareto-optimal routes |
| GET | `/api/route/timetable?from_id=X&to_id=Y&depart=...` | Timetable-based route |

### Analytics

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/delays/heatmap?country=DE` | Delay heatmap data |
| GET | `/api/delays/station/{stop_id}` | Station delay stats |

### Geometry

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/geometry/track?from_lat=...&to_lat=...` | Rail polyline from OSM |

### System

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/graph/stats` | Graph statistics |
| GET | `/api/health` | Health check |

---

## ⚙️ Configuration

All settings via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostGIS connection |
| `DEFAULT_OPERATOR` | `db` | Default rail operator |
| `CACHE_TTL_DEPARTURES` | `30` | Cache TTL for departures (seconds) |
| `CACHE_TTL_STATIONS` | `3600` | Cache TTL for stations (seconds) |
| `WS_BROADCAST_INTERVAL` | `15` | WebSocket update interval (seconds) |
| `OVERPASS_RATE_LIMIT` | `2` | Max Overpass requests per 10s |
| `DELAY_RETENTION_HOURS` | `24` | How long to keep delay data |

---

## 🧮 Algorithms

### Dijkstra
Standard shortest-path. Guaranteed optimal. Time complexity: $O((V+E) \log V)$.

### A* with Haversine
Dijkstra + admissible heuristic (haversine distance / max_speed). Same optimality guarantee, 2-5x faster in practice.

### Pareto Multi-Criteria
Returns the **Pareto frontier** — all non-dominated solutions across:
- Travel time
- Number of transfers
- Total distance

No single "best" route; the user picks their trade-off.

### Time-Expanded Graph
Nodes = `(station, timestamp)`. Edges = specific train departures. Finds routes using **actual timetable data**, not estimates.

---

## 📁 Data Sources

| Source | License | Usage |
|---|---|---|
| trainline-eu/stations | ODbL | Station database (10k+ EU stations) |
| transport.rest | ISC | DB real-time departures & trips |
| OpenStreetMap | ODbL | Rail track geometry via Overpass |
| transport.opendata.ch | Open | Swiss rail data |

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Setup

```bash
# Install dev dependencies
pip install -r requirements.txt
pip install ruff pytest pytest-asyncio httpx

# Run tests
pytest tests/ -v

# Lint
ruff check backend/
ruff format backend/
```

### Areas for Contribution
* 🌍 Add more operators (Trenitalia, Renfe, NS, PKP)
* 🧪 Write tests for optimizer algorithms
* 🎨 Improve frontend UI/UX
* 📖 Translate the interface
* 🐛 Fix issues from the tracker
* 📊 Add more analytics/visualizations

---

## 📝 License

MIT License — see `LICENSE` for details.

---

## 🙏 Acknowledgments

* **trainline-eu** for the open station dataset
* **derhuerst** for transport.rest & HAFAS docs
* **OpenStreetMap** contributors
* **Leaflet** for the mapping library

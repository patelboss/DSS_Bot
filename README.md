# SDSS Telegram Bot
### Spatial Decision Support System — MP Forest Department

A fully automated geospatial analysis bot that accepts polygon boundary uploads
and returns **Forest Cover**, **Elevation**, and **Area** statistics with a
cartographic map report — all running on a **100% free-tier** cloud stack.

---

## Architecture

```
User (Telegram)
     │ .geojson / .kml / .gpkg
     ▼
┌─────────────────────────────────┐
│       Koyeb Free Server         │
│   Python 3.11 + Docker          │
│                                 │
│  ┌───────────┐  ┌─────────────┐ │
│  │  Telegram │  │  Analysis   │ │
│  │  Bot      │→ │  Pipeline   │ │
│  │  (PTB 21) │  │  (rasterio) │ │
│  └───────────┘  └──────┬──────┘ │
│                        │        │
└────────────────────────┼────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   MongoDB Atlas    Supabase         Telegram
   (user logs)    (COG rasters)    (PNG report)
```

## Quick Start

### 1. Prerequisites

- Python 3.10+
- GDAL installed locally (`gdal-bin`, `libgdal-dev`)
- Accounts on: Telegram, MongoDB Atlas, Supabase, Koyeb, GitHub

---

### 2. Environment Setup

```bash
git clone https://github.com/YOUR_USERNAME/sdss-bot.git
cd sdss-bot
cp .env.example .env
# Fill in all values in .env
pip install -r requirements.txt
```

---

### 3. Prepare Raster Data (One-time)

Convert your GeoTIFFs to Cloud-Optimized format and upload to Supabase:

```bash
# Forest Cover Map
python data_prep/convert_to_cog.py --input /path/to/fcm.tif --layer FCM

# Digital Elevation Model
python data_prep/convert_to_cog.py --input /path/to/dem.tif --layer DEM
```

Your rasters should cover the study area (e.g., all of MP/CG).
Recommended sources:
- **FCM**: FSI Forest Cover Map (GeoTIFF from FSI GeoPortal)
- **DEM**: SRTM 30m DEM (download from USGS EarthExplorer or OpenTopography)

---

### 4. MongoDB Atlas Setup

1. Create a free M0 cluster at [cloud.mongodb.com](https://cloud.mongodb.com)
2. Database: `sdss`
3. Collections are auto-created on first run: `users`, `analysis_logs`
4. Whitelist all IPs (`0.0.0.0/0`) under Network Access (required for Koyeb)
5. Copy the connection string → paste into `.env` as `MONGO_URI`

---

### 5. Supabase Setup

1. Create a project at [supabase.com](https://supabase.com)
2. Go to **Storage** → Create a bucket named `raster-layers`
3. Set bucket to **Private** (signed URLs protect access)
4. Copy the **service_role key** → paste as `SUPABASE_SERVICE_KEY`

---

### 6. Telegram Bot Setup

1. Open Telegram → message **@BotFather**
2. `/newbot` → choose name and username
3. Copy the HTTP API token → paste as `TELEGRAM_BOT_TOKEN`

---

### 7. Koyeb Deployment

```bash
# Push to GitHub first
git add . && git commit -m "Initial SDSS bot" && git push
```

In Koyeb dashboard:
1. **New Service** → GitHub → select repo
2. **Builder**: Dockerfile
3. **Environment Variables**: paste all values from `.env`
4. **Port**: 8080 (health check)
5. Deploy → bot goes live in ~3 minutes

---

## Usage

| Command    | Description                              |
|------------|------------------------------------------|
| `/start`   | Welcome message + quick guide            |
| `/help`    | Detailed instructions                    |
| `/history` | Last 5 analysis runs                     |
| `/status`  | System health check (DB + Storage)       |
| _Upload_   | Send any `.geojson` / `.kml` / `.gpkg`  |

---

## Output Report

Each analysis produces:

1. **Telegram text summary** with:
   - Area in hectares
   - Dominant forest cover class + breakdown (%)
   - Elevation (min / max / mean in metres)
   - Slope (mean / max in degrees)

2. **PNG cartographic layout** with:
   - Polygon boundary on coordinate grid
   - North arrow + scale bar
   - Forest cover legend
   - Statistics table in footer

---

## File Structure

```
sdss/
├── main.py                    # Telegram bot + command handlers
├── config.py                  # All environment variables + constants
├── requirements.txt
├── Dockerfile
├── .env.example
├── modules/
│   ├── database.py            # MongoDB Atlas operations
│   ├── storage.py             # Supabase COG windowed streaming
│   ├── spatial_analysis.py    # Core geospatial computation pipeline
│   └── map_renderer.py        # Matplotlib cartographic layout
└── data_prep/
    └── convert_to_cog.py      # One-time COG conversion + upload utility
```

---

## Resource Budget (Free Tier)

| Service        | Usage                   | Free Limit              |
|----------------|-------------------------|-------------------------|
| Koyeb          | 1 web service           | 512 MB RAM, 2 GB SSD    |
| MongoDB Atlas  | User logs + history     | 512 MB storage          |
| Supabase       | 3 COG raster files      | 1 GB storage, 5 GB/mo   |
| Telegram       | Bot messaging           | Unlimited               |

---
[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?name=dss-bot&type=git&repository=patelboss%2FDSS_Bot&branch=main&builder=dockerfile&dockerfile=Dockerfile&instance_type=free&regions=was&instances_min=0&autoscaling_sleep_idle_delay=3900&env%5BAPI_HASH%5D=&env%5BAPI_ID%5D=&env%5BBOT_TOKEN%5D=&env%5BCHANNEL_ID%5D=&env%5BMONGO_URI%5D=&env%5BSUPABASE_SERVICE_KEY%5D=&env%5BSUPABASE_URL%5D=&ports=8080%3Bhttp%3B%2F&hc_protocol%5B8080%5D=tcp&hc_grace_period%5B8080%5D=5&hc_interval%5B8080%5D=30&hc_restart_limit%5B8080%5D=3&hc_timeout%5B8080%5D=5&hc_path%5B8080%5D=%2F&hc_method%5B8080%5D=get)
## License

For internal use by the Madhya Pradesh Forest Department.

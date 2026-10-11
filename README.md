# Employee Attendance & Analytics API

A robust, production-grade Attendance and Analytics REST API built with **Python 3.11+**, **FastAPI**, and **MongoDB**.

## Features

- **Attendance Management:** Punch-in and punch-out operations with overnight shift handling, 10-minute grace window calculations, exact floor late minutes, and overtime thresholds.
- **Concurrency & Idempotency:** Atomic CAS updates and unique compound index constraints preventing double-punches and race conditions under concurrent load.
- **Regularization & Audit Trail:** Manual corrections with complete audit trails tracking timestamp, author, reason, and field-level change history.
- **High-Performance Analytics:** 100% database-level MongoDB aggregation pipelines utilizing `$densify`, `$setWindowFields`, and `$rank` for monthly attendance, department summaries, late leaderboards, and moving-average trend analyses.
- **Query Optimization & Observability:** Optimized compound indexes and an `/admin/explain/{endpoint}` route providing live `executionStats`.

## Setup & Running Locally

### 1. Prerequisites
- Python 3.11 or newer
- MongoDB 6.0+ (Local Docker container or MongoDB Atlas)

### 2. Installation
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root (never committed to git):
```ini
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 5. Start Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive Swagger documentation is available at `http://localhost:8000/docs`.

## Project Structure
- `app/main.py` - Complete FastAPI application and aggregation logic
- `requirements.txt` - Project dependencies
- `REVIEW.md` - Analysis and fixes for starter code defects
- `DECISIONS.md` - Design decisions covering indexing, concurrency, and scaling
- `sample_seed.py` - Database seeder for sample data

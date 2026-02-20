#!/bin/bash
# restart.sh — Restart all SiteDoc services
#
# Usage:
#   ./restart.sh           # restart all services
#   ./restart.sh --build   # rebuild Next.js frontend before restarting
#
# Services managed:
#   - FastAPI (uvicorn)   → port 5000   → log: sitedoc-backend/uvicorn.log
#   - Celery worker       → backend q   → log: sitedoc-backend/celery_worker.log
#   - Celery Beat         → scheduler   → log: sitedoc-backend/celery_beat.log
#   - Next.js frontend    → port 5001   → log: sitedoc-frontend/next.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR"
FRONTEND_DIR="$SCRIPT_DIR/../sitedoc-frontend"
BUILD=false

# Parse args
for arg in "$@"; do
  [[ "$arg" == "--build" ]] && BUILD=true
done

# ── Colour helpers ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[restart]${NC} $*"; }
warn()    { echo -e "${YELLOW}[restart]${NC} $*"; }
section() { echo -e "\n${YELLOW}══ $* ══${NC}"; }

# ── Stop ───────────────────────────────────────────────────────────────────
section "Stopping services"

# Celery worker loop + workers (sitedoc only — matched by app name)
if pkill -f "start-beat.sh" 2>/dev/null; then info "Stopped start-beat.sh loop"; fi
if pkill -f "start-worker.sh" 2>/dev/null; then info "Stopped start-worker.sh loop"; fi
if pkill -f "celery.*src\.tasks\.base:celery_app worker" 2>/dev/null; then
  info "Stopped Celery worker(s)"
fi

# Celery Beat
if pkill -f "celery.*src\.tasks\.base:celery_app beat" 2>/dev/null; then
  info "Stopped Celery Beat"
fi

# FastAPI (uvicorn on port 5000)
if pkill -f "uvicorn src\.main:app.*--port 5000" 2>/dev/null || \
   pkill -f "uvicorn src\.main:app" 2>/dev/null; then
  info "Stopped FastAPI (uvicorn)"
fi

# Next.js on port 5001
if lsof -ti:5001 | xargs kill -9 2>/dev/null; then
  info "Stopped Next.js (port 5001)"
fi

# Give processes time to exit cleanly
sleep 3
info "All services stopped"

# ── Build frontend (optional) ──────────────────────────────────────────────
if $BUILD; then
  section "Building Next.js frontend"
  cd "$FRONTEND_DIR"
  npm run build
  info "Frontend build complete"
fi

# ── Start ──────────────────────────────────────────────────────────────────
section "Starting services"

# FastAPI
cd "$BACKEND_DIR"
nohup ./venv/bin/python -m uvicorn src.main:app \
  --port 5000 --host 0.0.0.0 --reload \
  >> uvicorn.log 2>&1 &
info "FastAPI started (PID: $!, log: sitedoc-backend/uvicorn.log)"

# Celery worker (via start-worker.sh for auto-restart on crash)
nohup bash start-worker.sh >> celery_worker.log 2>&1 &
info "Celery worker started (PID: $!, log: sitedoc-backend/celery_worker.log)"

# Celery Beat (via start-beat.sh for auto-restart on crash)
nohup bash start-beat.sh >> celery_beat.log 2>&1 &
info "Celery Beat started (PID: $!, log: sitedoc-backend/celery_beat.log)"

# Next.js
cd "$FRONTEND_DIR"
nohup node_modules/.bin/next start --port 5001 >> next.log 2>&1 &
info "Next.js started (PID: $!, log: sitedoc-frontend/next.log)"

# ── Health check ───────────────────────────────────────────────────────────
section "Waiting for services to be ready"
sleep 6

HEALTHY=true

if curl -sf http://localhost:5000/health > /dev/null 2>&1 || \
   curl -sf http://localhost:5000/api/v1/health > /dev/null 2>&1 || \
   curl -sf http://localhost:5000/ > /dev/null 2>&1; then
  info "FastAPI ✓ (port 5000)"
else
  warn "FastAPI may still be starting — check uvicorn.log"
  HEALTHY=false
fi

if curl -sf http://localhost:5001/ > /dev/null 2>&1; then
  info "Next.js ✓ (port 5001)"
else
  warn "Next.js may still be starting — check next.log"
  HEALTHY=false
fi

if ps aux | grep -q "[c]elery.*src\.tasks\.base:celery_app worker"; then
  info "Celery worker ✓"
else
  warn "Celery worker not detected — check celery_worker.log"
  HEALTHY=false
fi

if ps aux | grep -q "[c]elery.*src\.tasks\.base:celery_app beat"; then
  info "Celery Beat ✓"
else
  warn "Celery Beat not detected — check celery_beat.log"
  HEALTHY=false
fi

echo ""
if $HEALTHY; then
  echo -e "${GREEN}All services running.${NC}"
else
  echo -e "${YELLOW}Some services may still be starting. Check logs above.${NC}"
fi
echo ""
echo "  FastAPI:  http://localhost:5000"
echo "  Frontend: http://localhost:5001"
echo ""
echo "Logs:"
echo "  tail -f $BACKEND_DIR/uvicorn.log"
echo "  tail -f $BACKEND_DIR/celery_worker.log"
echo "  tail -f $BACKEND_DIR/celery_beat.log"
echo "  tail -f $FRONTEND_DIR/next.log"

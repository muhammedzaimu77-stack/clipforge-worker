# ClipForge — self-hosted video clipping worker

This is a companion to the hosted ClipForge studio. It provides a FastAPI API, Celery/Redis background queue, SQLite job storage, optional local file upload, yt-dlp URL ingestion, faster-whisper word timestamps, transparent heuristic moment ranking, FFmpeg vertical rendering, ASS-burned captions, SRT/VTT subtitle files, project status/history, retry and deletion endpoints.

**Honest implementation status:** this worker source is bundled in the Floot project as a self-hostable companion. Floot's hosted runtime does not run Docker, Python, FFmpeg, Redis or persistent Celery workers, so these files do not make media processing run in the hosted preview automatically. The heuristic score is a baseline, not a guarantee of views; Ollama scoring, face tracking, richer editor controls, ZIP export and social publishing are not implemented yet.

## Run locally with Docker

Requirements: Docker Engine with the Compose plugin, internet access to pull container/model files, and free disk space. 8 GB RAM is a practical starting point for the `base` Whisper model; larger models need more memory.

```sh
cd static/__dev/docs/clipforge-worker
cp .env.example .env
# Review limits and URL_DOWNLOAD_ENABLED before starting.
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health
```

Open the interactive API reference at http://localhost:8000/docs. This worker is separate from the hosted Floot preview. A frontend must be configured to call this API origin and `CORS_ORIGINS` must match that frontend's exact origin. Do not expose the API publicly without authentication, TLS, rate limiting and storage quotas.

## Deploy to Render

The `render.yaml` Blueprint is a starting point for Render. It runs the API and one Celery worker in the same Docker service so they share the same persistent disk for SQLite and media files; Redis is a separate managed Key Value service. This is a single-instance starter design, not a multi-instance production architecture. The `standard` web service, 10 GB persistent disk, and Redis service may incur charges. Review Render's current pricing and the Blueprint plan selections before approving deployment. The container starts both processes using `deploy/supervisord.conf`.

The source must be in a GitHub repository for a Render Blueprint deployment. After connecting the repository and importing `render.yaml`, wait for `/health` to report healthy, then copy the worker service URL and the generated `API_TOKEN` into ClipForge's Worker API URL and Worker API token fields. Set `CORS_ORIGINS` to the exact origin of the published frontend if it is not `https://floot.com`. Never paste the generated token into chat or commit it to Git.

## API

- `POST /api/metadata` — JSON `{ "url": "https://…", "permission_confirmed": true }`
- `POST /api/jobs` — same JSON; returns a job ID after queueing
- `POST /api/uploads` — multipart fields `file` and `permission_confirmed=true`
- `GET /api/jobs` — recent projects
- `GET /api/jobs/{id}` — project state and ranked clip metadata
- `GET /api/jobs/{id}/events` — Server-Sent Events status updates
- `POST /api/jobs/{id}/retry` — retry a failed job
- `GET /api/clips/{clip_id}/download` — rendered MP4
- `POST /api/clips/{clip_id}/render` — rerender an edited clip with start/end, aspect ratio, caption style, title and transcript
- `DELETE /api/jobs/{id}` — delete project record and its files
- `GET /health` — database/worker API health

## Configuration

Edit `.env` before starting. `WHISPER_MODEL` can be `tiny`, `base`, `small`, `medium` or `large-v3`; the default is `base`. The default image is CPU-oriented (`WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE_TYPE=int8`). CUDA use requires an appropriately configured GPU host and compatible CTranslate2/PyTorch-free faster-whisper runtime. Model weights download on first transcription. `WORKER_CONCURRENCY=1` avoids loading multiple models at once. `MAX_VIDEO_SECONDS` defaults to 7200 seconds; `MAX_UPLOAD_BYTES` defaults to 2 GiB; set `URL_DOWNLOAD_ENABLED=false` for uploads-only operation. Set `API_TOKEN` to a long random secret and enter the same value in the studio's worker token field to require Bearer authentication on all `/api/*` endpoints. Health checks and `/docs` remain unauthenticated. An empty token is suitable only for local testing; do not expose an unauthenticated API to the internet.

## Scoring and rendering

Base score = `0.22*hook + 0.16*emotion + 0.16*story + 0.14*novelty + 0.12*quotability + 0.10*opinion + 0.10*call_to_action`. Each component is 0–100 and the result is rounded/clamped to 0–100. The included fallback estimates these features from transcript text and punctuation only; it is not LLM analysis and does not predict actual reach. The optional audio adjustment is capped at ±5 but no audio classifier is connected yet.

FFmpeg runs with argument arrays, not shell interpolation. MP4 output uses H.264/AAC and centered crops for 9:16, 1:1 and 16:9. The current renderer uses a basic word-timed highlighted ASS caption style. SRT/VTT sidecars are written per clip. No watermark, intro or outro is added. Subject tracking, polished caption presets, per-word editing, split-screen and ZIP export remain future work.

## Safety and operational notes

- Only use videos you own or have permission to repurpose. Respect each site's terms and applicable law.
- Do not bypass DRM, private access, logins, age restrictions or geo-blocks. A source site's download restrictions can change and yt-dlp can stop working until updated.
- Configure a long random `API_TOKEN` to require Bearer authentication for `/api/*`. This is a shared-token gate, not multi-user accounts or per-project ownership. Health and API docs are intentionally unauthenticated; keep the service on localhost or behind a private authenticated proxy unless you add production-grade access control and rate limiting.
- SQLite is suitable for one worker on a local persistent volume. Use PostgreSQL and shared artifact storage for multi-worker production.
- Job status and transcript artifacts are persisted so retries can skip completed transcription. Celery retries transient runtime/OS errors twice. Permanent errors are stored as failed jobs.
- The included cleanup script is a helper to remove old artifact folders; set a retention policy deliberately and account for database rows/backups separately.

## Tests

```sh
cd worker && pytest -q
```

The tests cover scoring validation, segmentation bounds and URL checks against private-network targets. They have been added to the source bundle but have **not** been run inside a Docker host here. For an end-to-end check, upload a short video you own, wait for `completed` at `GET /api/jobs/{id}`, then download one of its clip URLs. Inspect worker logs with `docker compose logs -f worker`.

## Troubleshooting

- **Download rejected/blocked:** update yt-dlp, verify source availability and rights, or upload a permitted local file.
- **No speech:** try clear dialogue or a larger Whisper model.
- **Out of memory:** use `tiny`/`base`, keep worker concurrency at 1, or provision more RAM.
- **Job remains queued:** check `docker compose ps` and `docker compose logs redis worker api`.
- **FFmpeg error:** inspect worker logs, available disk, and the source codec.
- **CORS error:** set `CORS_ORIGINS` to the exact frontend origin and restart.
- **401 response:** ensure the studio's Worker API token exactly matches `API_TOKEN` in `.env` (without adding `Bearer ` to the token field).

## Roadmap

1. Optional local Ollama-based moment analysis and schema-validated scoring.
2. Multilingual translation, caption proofreading and more caption presets.
3. Face/speaker tracking, scene-aware crop and split-screen layouts.
4. ZIP export and full editing controls.
5. Authenticated multi-user workspaces and authorized social publishing.

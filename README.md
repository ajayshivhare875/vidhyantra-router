# Vidhyantra Router

OpenAI-compatible AI gateway powered **only by Vidhyantra** (free distribution).

## Models
- `codepro` → Code-X API, upstream `vidhyora-code` (OpenAI-native passthrough)
- `sol`, `ultra`, `quick`, … → Get API (translated to OpenAI format; any other model ID passes through as-is)

## API
- `POST /v1/chat/completions` — `Authorization: Bearer <vdr_key>`
- `GET  /v1/models`
- `GET  /get-key` — free key generation page
- `GET  /health`

## Local dev
```bash
cp .env.example .env   # fill keys
pip install -r requirements.txt
python backend.py
```

## Status
- [x] Scaffold + Code-X passthrough
- [ ] Confirm Get API response shape (live test pending) → tighten parser
- [ ] Deploy to Render + set env keys
- [ ] Verify end-to-end

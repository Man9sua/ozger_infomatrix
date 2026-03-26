# Ozger Infomatrix

Educational platform with:

- frontend client in `frontend/`
- Node.js backend in `backend/`
- Flask AI API in `backend/aiapi/`
- Supabase schema files in `backend/supabase_schema.sql` and `supabase_setup.sql`

## Requirements

- Node.js 18+
- Python 3.10+
- npm
- pip

## Environment

Create local secret files from templates:

- `backend/env.example` -> `backend/.env`
- `backend/aiapi/env.example` -> `backend/aiapi/.env`

Do not commit real `.env` files.

## Run Locally

1. Install backend dependencies:

```powershell
cd backend
npm install
```

2. Install AI API dependencies:

```powershell
cd backend/aiapi
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

3. Start services:

Terminal 1:

```powershell
cd backend
npm start
```

Terminal 2:

```powershell
cd backend/aiapi
venv\Scripts\activate
python app.py
```

Terminal 3:

```powershell
cd frontend
python -m http.server 8080
```

## Default Local URLs

- frontend: `http://localhost:8080`
- backend: `http://localhost:3001`
- AI API: `http://localhost:5000`

## Notes

- `.wrangler/`, virtual envs, caches, logs, and secret env files are ignored by git.
- Keep `env.example` files updated when required variables change.

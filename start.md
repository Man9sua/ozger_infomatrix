ozger - краткий запуск

Что нужно:
- Node.js 18+
- npm
- Python 3.9+
- pip

1. Установка зависимостей

Backend:
cd backend
npm install

AI API:
cd backend/aiapi
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

2. Настройка переменных

backend\.env
- база backend\env.example
- к заполнению:
  SUPABASE_URL
  SUPABASE_ANON_KEY
  SUPABASE_SERVICE_ROLE_KEY
  SESSION_SECRET

backend\aiapi\.env
- база в env.example
- к заполнению:
  GEMINI_API_KEY=your_key
  AI_TEACHER_PORT=5000
  FLASK_DEBUG=false

3. Запуск

Терминал 1:
cd backend\aiapi
venv\Scripts\activate
python app.py

Терминал 2:
cd backend
npm start

Терминал 3:
cd frontend
python -m http.server 8080

4. Адреса

- frontend: http://localhost:8080
- backend: http://localhost:3001
- ai api: http://localhost:5000

5. Если нужен Cloudflare deploy

cd ..
npm i -g wrangler
wrangler login
wrangler secret put SUPABASE_URL
wrangler secret put SUPABASE_ANON_KEY
wrangler secret put SUPABASE_SERVICE_ROLE_KEY
wrangler secret put AI_TEACHER_API_URL
wrangler deploy

Примечания:
- если фронт запускается через Live Server, проверь CORS_ORIGIN в backend\.env

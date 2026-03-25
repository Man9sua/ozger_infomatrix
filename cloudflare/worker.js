const DEFAULT_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization"
};

const PROFILE_FIELDS = [
  "username",
  "country",
  "city",
  "school",
  "class_number",
  "class_letter",
  "subject_combination",
  "subject1",
  "subject2",
  "avatar_url"
];

function jsonResponse(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...DEFAULT_HEADERS,
      ...headers
    }
  });
}

function withDefaultHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(DEFAULT_HEADERS)) {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
}

function resolveAiUpstreamBase(env) {
  const raw = String(env.AI_TEACHER_API_URL || "").trim().replace(/\/+$/, "");
  if (!raw) return "";
  return /\/api$/i.test(raw) ? raw : `${raw}/api`;
}

async function proxyAiTeacher(request, env) {
  const upstreamBase = resolveAiUpstreamBase(env);
  if (!upstreamBase) {
    return jsonResponse({ error: "AI Teacher API is not configured" }, 503);
  }

  const url = new URL(request.url);
  const tailPath = url.pathname.replace(/^\/api\/ai\/?/, "");
  const safeTailPath = tailPath ? `/${tailPath}` : "";
  const upstreamUrl = new URL(`${upstreamBase}${safeTailPath}`);
  upstreamUrl.search = url.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("origin");
  headers.delete("referer");
  headers.delete("content-length"); // let fetch re-calculate for streamed bodies

  const method = request.method.toUpperCase();
  const init = {
    method,
    headers,
    redirect: "follow"
  };
  if (method !== "GET" && method !== "HEAD") {
    init.body = request.body;
  }

  try {
    const upstreamRes = await fetch(upstreamUrl.toString(), init);
    const contentType = (upstreamRes.headers.get("content-type") || "").toLowerCase();

    if (upstreamRes.status === 404) {
      let upstreamMessage = "";
      if (contentType.includes("application/json")) {
        try {
          const payload = await upstreamRes.clone().json();
          upstreamMessage = String(payload?.message || payload?.error || "").toLowerCase();
        } catch {
          upstreamMessage = "";
        }
      }
      if (upstreamMessage.includes("application not found")) {
        return jsonResponse(
          {
            error: "AI backend is misconfigured (invalid AI_TEACHER_API_URL)",
            upstreamStatus: 404
          },
          503
        );
      }
    }

    if (upstreamRes.status >= 500 && !contentType.includes("application/json")) {
      return jsonResponse(
        {
          error: "AI provider temporary server error",
          upstreamStatus: upstreamRes.status
        },
        upstreamRes.status
      );
    }
    return withDefaultHeaders(upstreamRes);
  } catch (error) {
    return jsonResponse(
      {
        error: "Failed to reach AI Teacher API",
        details: String(error?.message || "Upstream request failed")
      },
      502
    );
  }
}

function getBearerToken(request) {
  const authHeader = request.headers.get("Authorization") || "";
  const match = authHeader.match(/^Bearer (.+)$/i);
  return match ? match[1] : null;
}

function supabaseHeaders(env, token, useServiceRole = false) {
  const headers = new Headers();
  const apiKey = useServiceRole
    ? env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_ANON_KEY
    : env.SUPABASE_ANON_KEY;
  headers.set("apikey", apiKey);
  headers.set("Authorization", `Bearer ${useServiceRole ? apiKey : token || ""}`);
  headers.set("Content-Type", "application/json");
  return headers;
}

async function getUserFromToken(env, token) {
  if (!token) return null;
  const res = await fetch(`${env.SUPABASE_URL}/auth/v1/user`, {
    headers: {
      apikey: env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${token}`
    }
  });
  if (!res.ok) return null;
  return res.json();
}

async function handleMaterials(request, env) {
  const token = getBearerToken(request);
  const user = await getUserFromToken(env, token);
  if (!user?.id) {
    return jsonResponse({ error: "No authentication token provided" }, 401);
  }

  const url = new URL(request.url);
  const restBase = `${env.SUPABASE_URL}/rest/v1/materials`;
  const headers = supabaseHeaders(env, token);

  if (request.method === "GET") {
    const subject = url.searchParams.get("subject");
    const type = url.searchParams.get("type");
    const params = new URLSearchParams();
    params.set("select", "*");
    params.set("user_id", `eq.${user.id}`);
    if (subject && subject !== "all") params.set("subject", `eq.${subject}`);
    if (type && type !== "all") params.set("type", `eq.${type}`);
    params.set("order", "created_at.desc");

    const res = await fetch(`${restBase}?${params.toString()}`, { headers });
    if (!res.ok) {
      return jsonResponse({ error: "Failed to fetch materials" }, 500);
    }
    const data = await res.json();
    return jsonResponse({ materials: data });
  }

  if (request.method === "POST") {
    const body = await request.json();
    const payload = {
      user_id: user.id,
      title: body.title,
      content: body.content,
      subject: body.subject,
      type: body.type,
      is_public: Boolean(body.is_public)
    };
    const res = await fetch(restBase, {
      method: "POST",
      headers: {
        ...Object.fromEntries(headers.entries()),
        Prefer: "return=representation"
      },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      return jsonResponse({ error: "Failed to create material" }, 500);
    }
    const data = await res.json();
    return jsonResponse({ material: data?.[0] || null }, 201);
  }

  return jsonResponse({ error: "Method not allowed" }, 405);
}

async function handleFavorites(request, env) {
  const token = getBearerToken(request);
  const user = await getUserFromToken(env, token);
  if (!user?.id) {
    return jsonResponse({ error: "No authentication token provided" }, 401);
  }

  const restBase = `${env.SUPABASE_URL}/rest/v1/favorites`;
  const headers = supabaseHeaders(env, token);

  if (request.method === "GET") {
    const params = new URLSearchParams();
    params.set("select", "*,materials(*)");
    params.set("user_id", `eq.${user.id}`);
    const res = await fetch(`${restBase}?${params.toString()}`, { headers });
    if (!res.ok) {
      return jsonResponse({ error: "Failed to fetch favorites" }, 500);
    }
    const data = await res.json();
    return jsonResponse({ favorites: data });
  }

  if (request.method === "POST") {
    const body = await request.json();
    const materialId = body.material_id;
    if (!materialId) {
      return jsonResponse({ error: "Material ID is required" }, 400);
    }

    const checkParams = new URLSearchParams();
    checkParams.set("select", "id");
    checkParams.set("user_id", `eq.${user.id}`);
    checkParams.set("material_id", `eq.${materialId}`);
    const existingRes = await fetch(`${restBase}?${checkParams.toString()}`, { headers });
    if (!existingRes.ok) {
      return jsonResponse({ error: "Failed to check favorite" }, 500);
    }
    const existing = await existingRes.json();

    if (existing.length > 0) {
      const deleteParams = new URLSearchParams();
      deleteParams.set("user_id", `eq.${user.id}`);
      deleteParams.set("material_id", `eq.${materialId}`);
      const deleteRes = await fetch(`${restBase}?${deleteParams.toString()}`, {
        method: "DELETE",
        headers
      });
      if (!deleteRes.ok) {
        return jsonResponse({ error: "Failed to remove favorite" }, 500);
      }
      return jsonResponse({ message: "Favorite removed" });
    }

    const insertRes = await fetch(restBase, {
      method: "POST",
      headers: {
        ...Object.fromEntries(headers.entries()),
        Prefer: "return=representation"
      },
      body: JSON.stringify({
        user_id: user.id,
        material_id: materialId
      })
    });
    if (!insertRes.ok) {
      return jsonResponse({ error: "Failed to add favorite" }, 500);
    }
    const data = await insertRes.json();
    return jsonResponse({ favorite: data?.[0] || null });
  }

  return jsonResponse({ error: "Method not allowed" }, 405);
}

async function handleProfile(request, env) {
  const token = getBearerToken(request);
  const user = await getUserFromToken(env, token);
  if (!user?.id) {
    return jsonResponse({ error: "No authentication token provided" }, 401);
  }

  const restBase = `${env.SUPABASE_URL}/rest/v1/profiles`;
  const headers = supabaseHeaders(env, token);

  if (request.method === "GET") {
    const params = new URLSearchParams();
    params.set("select", "*");
    params.set("user_id", `eq.${user.id}`);
    params.set("limit", "1");
    const res = await fetch(`${restBase}?${params.toString()}`, { headers });
    if (!res.ok) {
      return jsonResponse({ error: "Failed to fetch profile" }, 500);
    }
    const data = await res.json();
    return jsonResponse({ profile: data?.[0] || null });
  }

  if (request.method === "PUT") {
    const body = await request.json();
    const update = { user_id: user.id, updated_at: new Date().toISOString() };
    for (const field of PROFILE_FIELDS) {
      if (body[field] !== undefined) {
        update[field] = body[field];
      }
    }

    const params = new URLSearchParams();
    params.set("on_conflict", "user_id");
    const res = await fetch(`${restBase}?${params.toString()}`, {
      method: "POST",
      headers: {
        ...Object.fromEntries(headers.entries()),
        Prefer: "resolution=merge-duplicates,return=representation"
      },
      body: JSON.stringify(update)
    });
    if (!res.ok) {
      return jsonResponse({ error: "Failed to update profile" }, 500);
    }
    const data = await res.json();
    return jsonResponse({ profile: data?.[0] || null });
  }

  return jsonResponse({ error: "Method not allowed" }, 405);
}

async function handleApi(request, env) {
  const url = new URL(request.url);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: DEFAULT_HEADERS });
  }

  if (url.pathname === "/health") {
    return jsonResponse({ status: "OK", timestamp: new Date().toISOString() });
  }

  if (url.pathname === "/api/config" && request.method === "GET") {
    const aiBase = resolveAiUpstreamBase(env);
    return jsonResponse({
      supabaseUrl: env.SUPABASE_URL,
      supabaseAnonKey: env.SUPABASE_ANON_KEY,
      aiTeacherApiUrl: aiBase || ""
    }, 200, { "Cache-Control": "no-store" });
  }

  if (url.pathname === "/api/ai" || url.pathname.startsWith("/api/ai/")) {
    return proxyAiTeacher(request, env);
  }

  if (url.pathname === "/api/materials") {
    return handleMaterials(request, env);
  }

  if (url.pathname === "/api/favorites") {
    return handleFavorites(request, env);
  }

  if (url.pathname === "/api/profile") {
    return handleProfile(request, env);
  }

  return jsonResponse({ error: "Route not found" }, 404);
}

async function serveAsset(request, env) {
  if (!env.ASSETS?.fetch) {
    return new Response("Assets binding not configured", { status: 500 });
  }

  let response = await env.ASSETS.fetch(request);
  if (response.status === 404) {
    const url = new URL(request.url);
    url.pathname = "/index.html";
    response = await env.ASSETS.fetch(new Request(url.toString(), { method: "GET" }));
  }
  return response;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
      return handleApi(request, env);
    }
    return serveAsset(request, env);
  }
};

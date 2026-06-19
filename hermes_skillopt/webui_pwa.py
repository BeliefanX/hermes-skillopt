from __future__ import annotations

"""PWA assets and response helpers for the FastAPI/React WebUI."""

import json
import zlib
from typing import Any

PWA_THEME_COLOR = "#f6f3ee"
PWA_BACKGROUND_COLOR = "#f6f3ee"
PWA_CACHE_NAME = "hermes-skillopt-pwa-static-v1"
PWA_ASSET_PATHS = (
    "/manifest.webmanifest",
    "/offline.html",
    "/icons/skillopt-icon-192.png",
    "/icons/skillopt-icon-512.png",
    "/icons/apple-touch-icon.png",
    "/favicon.svg",
)

PWA_HEAD = f"""
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="{PWA_THEME_COLOR}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Hermes SkillOpt">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>
(() => {{
  if ("serviceWorker" in navigator && window.isSecureContext) {{
    window.addEventListener("load", () => {{
      navigator.serviceWorker.register("/sw.js", {{ scope: "/" }}).catch(() => {{}});
    }});
  }}
}})();
</script>
""".strip()

PWA_STARTUP_JS = f"""
() => {{
  const head = document.head || document.getElementsByTagName("head")[0];
  if (!head) return [];

  const ensureMeta = (name, content) => {{
    let meta = head.querySelector(`meta[name="${{name}}"]`);
    if (!meta) {{
      meta = document.createElement("meta");
      meta.setAttribute("name", name);
      head.appendChild(meta);
    }}
    meta.setAttribute("content", content);
  }};

  const ensureLink = (rel, href, attrs = {{}}) => {{
    head.querySelectorAll(`link[rel="${{rel}}"]`).forEach((link) => {{
      if (link.getAttribute("href") !== href) link.remove();
    }});
    let link = head.querySelector(`link[rel="${{rel}}"][href="${{href}}"]`);
    if (!link) {{
      link = document.createElement("link");
      link.setAttribute("rel", rel);
      link.setAttribute("href", href);
      head.appendChild(link);
    }}
    Object.entries(attrs).forEach(([key, value]) => link.setAttribute(key, value));
  }};

  ensureMeta("viewport", "width=device-width, initial-scale=1, viewport-fit=cover");
  ensureMeta("theme-color", "{PWA_THEME_COLOR}");
  ensureMeta("mobile-web-app-capable", "yes");
  ensureMeta("apple-mobile-web-app-capable", "yes");
  ensureMeta("apple-mobile-web-app-title", "Hermes SkillOpt");
  ensureMeta("apple-mobile-web-app-status-bar-style", "black-translucent");
  ensureLink("manifest", "/manifest.webmanifest");
  ensureLink("apple-touch-icon", "/icons/apple-touch-icon.png");
  ensureLink("icon", "/favicon.svg", {{ type: "image/svg+xml" }});

  if ("serviceWorker" in navigator && window.isSecureContext) {{
    navigator.serviceWorker.register("/sw.js", {{ scope: "/" }}).catch(() => {{}});
  }}
  return [];
}}
""".strip()


def pwa_head_html() -> str:
    return PWA_HEAD


def pwa_startup_js() -> str:
    return PWA_STARTUP_JS


def pwa_manifest() -> dict[str, Any]:
    return {
        "id": "/?hermes-skillopt-pwa",
        "name": "Hermes SkillOpt",
        "short_name": "SkillOpt",
        "description": "Local staged-only Hermes SkillOpt review console.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": PWA_THEME_COLOR,
        "background_color": PWA_BACKGROUND_COLOR,
        "icons": [
            {"src": "/icons/skillopt-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/skillopt-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        ],
    }


def offline_html() -> str:
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="theme-color" content="#f6f3ee"><title>Hermes SkillOpt offline</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f3ee;color:#25211b;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:24px}main{max-width:560px;border:1px solid #ded7cc;border-radius:20px;padding:24px;background:rgba(255,255,255,.76);box-shadow:0 18px 55px rgba(54,45,31,.10)}p{color:#6f6a61;line-height:1.6}</style></head><body><main><h1>Hermes SkillOpt is offline</h1><p>This fallback is static and contains no run IDs, profile paths, status, artifacts, or cached operation results. Reconnect to the local WebUI to review staged runs.</p></main></body></html>"""


def service_worker_js() -> str:
    assets = json.dumps(list(PWA_ASSET_PATHS), ensure_ascii=False)
    return f"""
const CACHE_NAME = {json.dumps(PWA_CACHE_NAME)};
const STATIC_ASSETS = new Set({assets});
const NETWORK_ONLY_PREFIXES = ["/api/", "/run/", "/queue/", "/file=", "/file/", "/call/", "/cancel", "/reset"];

self.addEventListener("install", (event) => {{
  event.waitUntil((async () => {{
    const cache = await caches.open(CACHE_NAME);
    for (const path of STATIC_ASSETS) {{
      const response = await fetch(path, {{ cache: "reload", credentials: "same-origin" }});
      if (response.ok) await cache.put(path, response.clone());
    }}
    await self.skipWaiting();
  }})());
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil((async () => {{
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)));
    await self.clients.claim();
  }})());
}});

function isStaticAsset(url) {{
  return url.origin === self.location.origin && STATIC_ASSETS.has(url.pathname);
}}

function isDynamicOrPrivate(url) {{
  if (url.origin !== self.location.origin) return true;
  if (url.pathname === "/" || url.pathname === "") return true;
  if (url.pathname.endsWith("/")) return true;
  return NETWORK_ONLY_PREFIXES.some((prefix) => url.pathname.startsWith(prefix));
}}

self.addEventListener("fetch", (event) => {{
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  if (isStaticAsset(url)) {{
    event.respondWith((async () => {{
      const cached = await caches.match(url.pathname);
      try {{
        const response = await fetch(request, {{ cache: "reload", credentials: "same-origin" }});
        if (response.ok) {{
          const cache = await caches.open(CACHE_NAME);
          await cache.put(url.pathname, response.clone());
        }}
        return response;
      }} catch (err) {{
        return cached || Response.error();
      }}
    }})());
    return;
  }}

  if (isDynamicOrPrivate(url)) {{
    event.respondWith((async () => {{
      try {{
        return await fetch(request, {{ cache: "no-store", credentials: "same-origin" }});
      }} catch (err) {{
        if (request.mode === "navigate") {{
          const offline = await caches.match("/offline.html");
          if (offline) return offline;
        }}
        throw err;
      }}
    }})());
  }}
}});
""".strip()


def favicon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0b0c0f"/><path d="M14 18h36M14 32h26M14 46h18" stroke="#f4f4f5" stroke-width="6" stroke-linecap="round"/><circle cx="48" cy="44" r="7" fill="#9ca3af"/></svg>"""


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + kind + payload + zlib.crc32(kind + payload).to_bytes(4, "big")


def pwa_icon_png(size: int) -> bytes:
    if size not in {180, 192, 512}:
        raise ValueError("unsupported PWA icon size")
    bg = (11, 12, 15, 255)
    fg = (244, 244, 245, 255)
    accent = (156, 163, 175, 255)
    rows = []
    radius = max(18, size // 7)
    margin = max(18, size // 9)
    stroke = max(10, size // 18)
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            px = bg
            if (x < radius and y < radius and (x - radius) ** 2 + (y - radius) ** 2 > radius ** 2) or (x >= size - radius and y < radius and (x - (size - radius - 1)) ** 2 + (y - radius) ** 2 > radius ** 2) or (x < radius and y >= size - radius and (x - radius) ** 2 + (y - (size - radius - 1)) ** 2 > radius ** 2) or (x >= size - radius and y >= size - radius and (x - (size - radius - 1)) ** 2 + (y - (size - radius - 1)) ** 2 > radius ** 2):
                px = bg
            for idx, width in enumerate((size - 2 * margin, int(size * 0.58), int(size * 0.42))):
                ly = margin + idx * int(size * 0.22)
                if margin <= x <= margin + width and ly <= y <= ly + stroke:
                    px = fg
            cx, cy, rr = int(size * 0.75), int(size * 0.72), max(12, size // 11)
            if (x - cx) ** 2 + (y - cy) ** 2 <= rr ** 2:
                px = accent
            row.extend(px)
        rows.append(bytes(row))
    raw = b"".join(rows)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", size.to_bytes(4, "big") + size.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")


def pwa_response_headers(*, static_asset: bool = False) -> dict[str, str]:
    # Explicit no-store keeps browsers from passively storing private/state pages;
    # the service worker itself only stores the static allowlist above.
    cache_control = "public, max-age=3600" if static_asset else "no-store"
    return {"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"}

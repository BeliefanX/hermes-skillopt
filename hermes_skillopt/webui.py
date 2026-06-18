from __future__ import annotations

"""Hermes-native WebUI for hermes-skillopt.

Gradio is intentionally imported lazily so normal plugin imports/tests do not
require the optional web UI dependency.
"""

import argparse
import inspect
import json
import zlib
import sys
from pathlib import Path
from typing import Any

from hermes_skillopt import core

INSTALL_HINT = (
    "Gradio is required for the hermes-skillopt WebUI. Install the optional "
    "dependency with: python3 -m pip install 'hermes-skillopt[webui]' or "
    "python3 -m pip install gradio"
)
MAX_TEXT_CHARS = 20_000
PWA_THEME_COLOR = "#0b0c0f"
PWA_BACKGROUND_COLOR = "#0b0c0f"
PWA_CACHE_NAME = "hermes-skillopt-pwa-static-v1"
PWA_ASSET_PATHS = (
    "/manifest.webmanifest",
    "/offline.html",
    "/icons/skillopt-icon-192.png",
    "/icons/skillopt-icon-512.png",
    "/icons/apple-touch-icon.png",
    "/favicon.svg",
)
ALLOWED_ARTIFACTS = {
    "manifest.json",
    "checkpoint.json",
    "report.md",
    "diff.patch",
    "gate_results.json",
    "candidate_summary.json",
    "rejected_edits.jsonl",
    "proposed_SKILL.md",
    "best_skill.md",
}

LANGUAGES = {"en": "English", "zh": "中文"}

UI_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Hermes SkillOpt",
        "eyebrow": "Local safety operations console",
        "hero": "# Hermes SkillOpt\nA calm review surface for staged SKILL.md optimization runs.",
        "intro": "SkillOpt treats SKILL.md as trainable state while keeping the Hermes executor, staged artifacts, profile isolation, and adopt/rollback guards outside the optimizer. WebUI runs are staged-only unless a separate guarded writeback action is explicitly confirmed.",
        "card_safety_title": "Safety model",
        "card_safety_body": "Optimizer backends only propose bounded edits. Adoption and rollback stay behind typed confirmations and core integrity checks.",
        "card_review_title": "Review posture",
        "card_review_body": "Artifact review reads a fixed allowlist from the selected staging run; unsafe paths and symlink escapes are rejected.",
        "card_profile_title": "Profile boundary",
        "card_profile_body": "Read/run operations may use HERMES_HOME override. Live writeback intentionally uses the active Hermes profile.",
        "language": "Language",
        "home_label": "HERMES_HOME override for staged read/run operations",
        "home_info": "Optional. Defaults to active HERMES_HOME/~/.hermes. Adopt/Rollback ignore this override.",
        "home_placeholder": "Optional path for staged reads/runs",
        "tab_status": "Status",
        "tab_run": "Run staged optimization",
        "tab_review": "Review artifacts",
        "tab_adopt": "Adopt",
        "tab_rollback": "Rollback",
        "tab_upstream": "Upstream",
        "refresh_status": "Refresh status",
        "skill_label": "Skill name/path",
        "skill_placeholder": "Required if multiple skills exist",
        "query_label": "Query/session search",
        "query_placeholder": "Optional",
        "eval_label": "Curated eval file",
        "eval_info": "Optional JSONL/JSON under HERMES_HOME. Expected/forbidden keywords enable deterministic scoring.",
        "lookback": "Lookback days",
        "limit": "Harvest limit",
        "iterations": "Iterations",
        "edit_budget": "Edit budget",
        "candidate_count": "Candidate count",
        "backend": "Backend",
        "optimizer_backend": "Optimizer backend",
        "target_backend": "Target backend",
        "gate_mode": "Gate mode",
        "allow_mock": "Allow mock fallback (smoke/tests only)",
        "resume_run_id": "Resume run ID",
        "resume_placeholder": "Reuse completed checkpoint if inputs match",
        "run_button": "Run full cycle (staged only)",
        "run_note": "No automatic adoption. Results are written to staging for review.",
        "review_run_id": "Run ID",
        "review_placeholder": "Blank = latest staged run",
        "review_button": "Review selected run",
        "adopt_copy": "Adopt writes only to the active Hermes profile. The HERMES_HOME override textbox is ignored for live writeback.",
        "rollback_copy": "Rollback writes only to the active Hermes profile. The HERMES_HOME override textbox is ignored for live writeback.",
        "confirmation": "Confirmation",
        "force_guard": "Force sha guard override",
        "adopt_button": "Adopt staged proposal",
        "rollback_button": "Rollback adopted run",
        "upstream_copy": "Upstream update uses the active profile's canonical clone only; the HERMES_HOME override textbox is ignored for update writeback.",
        "fetch_only": "Fetch only",
        "upstream_status": "Upstream status",
        "parity_status": "Benchmark/parity status (read-only)",
        "upstream_update": "Update/fetch pinned upstream",
    },
    "zh": {
        "title": "Hermes SkillOpt",
        "eyebrow": "本地安全运维控制台",
        "hero": "# Hermes SkillOpt\n用于审查暂存 SKILL.md 优化运行的冷静界面。",
        "intro": "SkillOpt 将 SKILL.md 视为可训练状态，同时把 Hermes 执行器、暂存产物、Profile 隔离以及采用/回滚保护保留在优化器之外。WebUI 默认只写入暂存区；实时写回必须通过单独的受保护确认操作完成。",
        "card_safety_title": "安全模型",
        "card_safety_body": "优化后端只提出有边界的编辑。采用与回滚仍需精确输入确认，并经过核心完整性检查。",
        "card_review_title": "审查姿态",
        "card_review_body": "产物审查只读取所选暂存运行中的固定白名单文件；不安全路径与符号链接逃逸会被拒绝。",
        "card_profile_title": "Profile 边界",
        "card_profile_body": "读取/运行可使用 HERMES_HOME 覆盖。实时写回始终使用当前激活的 Hermes Profile。",
        "language": "语言",
        "home_label": "用于暂存读取/运行的 HERMES_HOME 覆盖",
        "home_info": "可选。默认使用当前 HERMES_HOME/~/.hermes。采用/回滚会忽略此覆盖。",
        "home_placeholder": "用于暂存读取/运行的可选路径",
        "tab_status": "状态",
        "tab_run": "运行暂存优化",
        "tab_review": "审查产物",
        "tab_adopt": "采用",
        "tab_rollback": "回滚",
        "tab_upstream": "上游",
        "refresh_status": "刷新状态",
        "skill_label": "Skill 名称/路径",
        "skill_placeholder": "存在多个 skill 时必填",
        "query_label": "查询/session 搜索",
        "query_placeholder": "可选",
        "eval_label": "人工评测文件",
        "eval_info": "可选，位于 HERMES_HOME 下的 JSONL/JSON。expected/forbidden 关键词可用于确定性评分。",
        "lookback": "回溯天数",
        "limit": "采集上限",
        "iterations": "迭代次数",
        "edit_budget": "编辑预算",
        "candidate_count": "候选数量",
        "backend": "后端",
        "optimizer_backend": "优化器后端",
        "target_backend": "目标后端",
        "gate_mode": "门禁模式",
        "allow_mock": "允许 mock fallback（仅冒烟/测试）",
        "resume_run_id": "恢复运行 ID",
        "resume_placeholder": "输入匹配时复用已完成 checkpoint",
        "run_button": "运行完整周期（仅暂存）",
        "run_note": "不会自动采用。结果写入暂存区等待审查。",
        "review_run_id": "运行 ID",
        "review_placeholder": "留空 = 最新暂存运行",
        "review_button": "审查所选运行",
        "adopt_copy": "采用只写入当前激活的 Hermes Profile。HERMES_HOME 覆盖输入框不会用于实时写回。",
        "rollback_copy": "回滚只写入当前激活的 Hermes Profile。HERMES_HOME 覆盖输入框不会用于实时写回。",
        "confirmation": "确认文本",
        "force_guard": "强制覆盖 sha 保护",
        "adopt_button": "采用暂存方案",
        "rollback_button": "回滚已采用运行",
        "upstream_copy": "上游更新仅使用当前 Profile 的 canonical clone；HERMES_HOME 覆盖输入框不会用于更新写回。",
        "fetch_only": "仅 fetch",
        "upstream_status": "上游状态",
        "parity_status": "Benchmark/parity 状态（只读）",
        "upstream_update": "更新/fetch 固定上游",
    },
}

WEBUI_CSS = """
:root { --skillopt-bg: #0b0c0f; --skillopt-panel: #111318; --skillopt-border: #272a32; --skillopt-muted: #a1a1aa; --skillopt-text: #f4f4f5; --skillopt-accent: #9ca3af; --skillopt-code-bg: #171a21; --skillopt-code-border: #343844; color-scheme: dark; }
html, body { min-height: 100%; width: 100%; max-width: 100vw; overflow-x: hidden !important; }
body { margin: 0; padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left); }
gradio-app, .gradio-container { display: block; width: 100% !important; max-width: min(1180px, 100vw) !important; min-width: 0 !important; box-sizing: border-box !important; }
.gradio-container { margin: 0 auto !important; padding-left: max(8px, env(safe-area-inset-left)) !important; padding-right: max(8px, env(safe-area-inset-right)) !important; padding-bottom: max(10px, env(safe-area-inset-bottom)) !important; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important; }
body, .gradio-container { background: radial-gradient(circle at top left, rgba(156, 163, 175, 0.12), transparent 32rem), var(--skillopt-bg) !important; color: var(--skillopt-text) !important; }
.skillopt-shell { width: 100%; max-width: 100%; min-width: 0; padding: max(18px, env(safe-area-inset-top)) 8px 10px; box-sizing: border-box; }
.skillopt-eyebrow { color: var(--skillopt-accent); letter-spacing: .14em; text-transform: uppercase; font-size: 12px; font-weight: 700; margin-bottom: 10px; }
.skillopt-hero { width: 100%; max-width: 100%; min-width: 0; border: 1px solid var(--skillopt-border); background: linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.025)); border-radius: 22px; padding: 28px; box-shadow: 0 24px 70px rgba(0,0,0,.28); }
.skillopt-hero h1 { letter-spacing: -.04em; font-size: 42px; margin-bottom: 6px; }
.skillopt-hero p, .skillopt-card p { color: var(--skillopt-muted); line-height: 1.6; }
.skillopt-cards { display: grid; width: 100%; max-width: 100%; min-width: 0; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 16px 0 18px; }
.skillopt-card { width: 100%; max-width: 100%; min-width: 0; border: 1px solid var(--skillopt-border); background: rgba(17,19,24,.76); border-radius: 16px; padding: 16px; }
.skillopt-card h3 { margin: 0 0 6px; font-size: 15px; }
.skillopt-note { color: var(--skillopt-muted); font-size: 13px; }
.gradio-container *, .skillopt-card, .skillopt-hero, textarea, input, pre, code { box-sizing: border-box; min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.gradio-container pre, .gradio-container code, .cm-content, .cm-line { white-space: pre-wrap !important; overflow-wrap: anywhere !important; }
.gradio-container .main, .gradio-container main.contain, .gradio-container .contain, .gradio-container .wrap, .gradio-container .form, .gradio-container .block, .gradio-container .panel, .gradio-container .tabs, .gradio-container .tabitem, .gradio-container .column, .gradio-container .row, .gradio-container .prose { width: 100%; max-width: 100% !important; min-width: 0 !important; box-sizing: border-box !important; }
.gradio-container textarea, .gradio-container input, .gradio-container select, .gradio-container label, .gradio-container .input, .gradio-container .output { max-width: 100% !important; min-width: 0 !important; }
.skillopt-settings-row { align-items: stretch !important; gap: 12px !important; }
.skillopt-settings-row > * { min-width: 0 !important; }
.gradio-container .tabs [role="tablist"], .gradio-container .tab-nav, .gradio-container div[role="tablist"], .tab-nav { display: flex !important; flex-wrap: nowrap !important; gap: 8px !important; width: 100% !important; max-width: 100% !important; min-width: 0 !important; overflow-x: auto !important; overflow-y: hidden !important; overscroll-behavior-x: contain; scroll-snap-type: x proximity; -webkit-overflow-scrolling: touch; padding: 4px 2px 8px !important; scrollbar-width: none; }
.gradio-container .tabs [role="tablist"]::-webkit-scrollbar, .gradio-container .tab-nav::-webkit-scrollbar, .gradio-container div[role="tablist"]::-webkit-scrollbar, .tab-nav::-webkit-scrollbar { display: none; }
.gradio-container .tabs [role="tablist"] > button[role="tab"], .gradio-container .tab-nav button[role="tab"], .gradio-container div[role="tablist"] > button[role="tab"], .gradio-container button[role="tab"], .gradio-container [role="tab"], .tab-nav button { flex: 0 0 auto !important; width: auto !important; min-width: max-content !important; max-width: none !important; font-weight: 650 !important; white-space: nowrap !important; border-radius: 999px !important; padding: 9px 14px !important; line-height: 1.2 !important; text-overflow: clip !important; overflow: visible !important; scroll-snap-align: start; }
.gradio-container [role="tab"] * { white-space: nowrap !important; overflow: visible !important; text-overflow: clip !important; }
.skillopt-status-md, .skillopt-result-md, .gradio-container .prose { color: var(--skillopt-text) !important; line-height: 1.58 !important; }
.skillopt-status-md .prose, .skillopt-result-md .prose { border: 1px solid rgba(255,255,255,.08); background: rgba(17,19,24,.68); border-radius: 16px; padding: 14px 16px; }
.skillopt-status-md p, .skillopt-status-md li, .skillopt-result-md p, .skillopt-result-md li, .gradio-container .prose p, .gradio-container .prose li { color: #e4e4e7 !important; }
.skillopt-status-md ul, .skillopt-status-md ol, .skillopt-result-md ul, .skillopt-result-md ol, .gradio-container .prose ul, .gradio-container .prose ol { padding-left: 1.15rem !important; margin: .45rem 0 .75rem !important; }
.skillopt-status-md h1, .skillopt-status-md h2, .skillopt-status-md h3, .skillopt-result-md h1, .skillopt-result-md h2, .skillopt-result-md h3, .gradio-container .prose h1, .gradio-container .prose h2, .gradio-container .prose h3 { color: #fafafa !important; letter-spacing: -.02em; }
.skillopt-status-md code, .skillopt-result-md code, .gradio-container .prose code { color: #f4f4f5 !important; background: var(--skillopt-code-bg) !important; border: 1px solid var(--skillopt-code-border) !important; border-radius: 7px !important; padding: .12rem .36rem !important; white-space: pre-wrap !important; overflow-wrap: anywhere !important; word-break: break-word !important; }
.skillopt-status-md pre, .skillopt-result-md pre, .gradio-container .prose pre { color: #f4f4f5 !important; background: #0f1117 !important; border: 1px solid var(--skillopt-code-border) !important; border-radius: 12px !important; padding: 12px !important; overflow-x: auto !important; white-space: pre-wrap !important; }
.skillopt-status-md pre code, .skillopt-result-md pre code, .gradio-container .prose pre code { border: 0 !important; padding: 0 !important; background: transparent !important; }
button.primary, .primary button { border-radius: 12px !important; }
textarea, input, .wrap { border-radius: 12px !important; }
@media (max-width: 860px) { gradio-app, .gradio-container, .gradio-container .main, .gradio-container main.contain, .gradio-container .wrap, .gradio-container .column, .gradio-container .block, .skillopt-shell, .skillopt-hero, .skillopt-cards, .skillopt-card { width: min(100%, 100vw) !important; max-width: 100vw !important; min-width: 0 !important; } .gradio-container { margin-inline: 0 !important; padding-inline: max(8px, env(safe-area-inset-left)) max(8px, env(safe-area-inset-right)) !important; } .skillopt-cards { grid-template-columns: minmax(0, 1fr); } .skillopt-hero { padding: 20px; border-radius: 18px; } .skillopt-hero h1 { font-size: 32px; } }
@media (max-width: 640px) { .skillopt-settings-row { flex-direction: column !important; } .skillopt-settings-row > *, .skillopt-settings-row .form, .skillopt-settings-row .block { width: 100% !important; max-width: 100% !important; flex: 1 1 auto !important; } .tab-nav button { padding-inline: 12px !important; font-size: 14px !important; } .skillopt-status-md .prose, .skillopt-result-md .prose { padding: 12px; border-radius: 14px; } }
@media (max-width: 520px) { .skillopt-shell { padding-top: max(14px, env(safe-area-inset-top)); padding-inline: 0; } .skillopt-hero { padding: 16px; } .skillopt-hero h1 { font-size: clamp(24px, 7vw, 28px); } button { min-height: 42px; } }
"""

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
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="theme-color" content="#0b0c0f"><title>Hermes SkillOpt offline</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0c0f;color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:24px}main{max-width:560px;border:1px solid #272a32;border-radius:20px;padding:24px;background:#111318}p{color:#a1a1aa;line-height:1.6}</style></head><body><main><h1>Hermes SkillOpt is offline</h1><p>This fallback is static and contains no run IDs, profile paths, status, artifacts, or cached operation results. Reconnect to the local WebUI to review staged runs.</p></main></body></html>"""


def service_worker_js() -> str:
    assets = json.dumps(list(PWA_ASSET_PATHS), ensure_ascii=False)
    return f"""
const CACHE_NAME = {json.dumps(PWA_CACHE_NAME)};
const STATIC_ASSETS = new Set({assets});
const NETWORK_ONLY_PREFIXES = ["/api/", "/run/", "/queue/", "/file=", "/file/", "/gradio_api/", "/component_server/", "/call/", "/cancel", "/reset"];

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
            # rounded-corner transparent-ish dark square stays deterministic without alpha holes
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


def ui_text(lang: str | None, key: str) -> str:
    code = lang if lang in UI_TEXT else "en"
    return UI_TEXT[code].get(key) or UI_TEXT["en"].get(key, key)


def hero_markdown(lang: str | None = "en") -> str:
    return f"""<div class=\"skillopt-shell\"><div class=\"skillopt-eyebrow\">{ui_text(lang, 'eyebrow')}</div><div class=\"skillopt-hero\">{ui_text(lang, 'hero')}\n\n{ui_text(lang, 'intro')}</div></div>"""


def safety_cards_markdown(lang: str | None = "en") -> str:
    cards = []
    for prefix in ("card_safety", "card_review", "card_profile"):
        cards.append(f"<div class=\"skillopt-card\"><h3>{ui_text(lang, prefix + '_title')}</h3><p>{ui_text(lang, prefix + '_body')}</p></div>")
    return "<div class=\"skillopt-cards\">" + "".join(cards) + "</div>"


def _gr_update(gr: Any, **kwargs: Any) -> Any:
    update = getattr(gr, "update", None)
    if callable(update):
        return update(**kwargs)
    return kwargs


def language_updates(gr: Any, lang: str | None) -> tuple[Any, ...]:
    """Return component updates for the language selector without touching internal values."""
    return (
        hero_markdown(lang),
        safety_cards_markdown(lang),
        _gr_update(gr, label=ui_text(lang, "home_label"), info=ui_text(lang, "home_info"), placeholder=ui_text(lang, "home_placeholder")),
        _gr_update(gr, value=ui_text(lang, "refresh_status")),
        _gr_update(gr, label=ui_text(lang, "skill_label"), placeholder=ui_text(lang, "skill_placeholder")),
        _gr_update(gr, label=ui_text(lang, "query_label"), placeholder=ui_text(lang, "query_placeholder")),
        _gr_update(gr, label=ui_text(lang, "eval_label"), info=ui_text(lang, "eval_info")),
        _gr_update(gr, label=ui_text(lang, "lookback")),
        _gr_update(gr, label=ui_text(lang, "limit")),
        _gr_update(gr, label=ui_text(lang, "iterations")),
        _gr_update(gr, label=ui_text(lang, "edit_budget")),
        _gr_update(gr, label=ui_text(lang, "candidate_count")),
        _gr_update(gr, label=ui_text(lang, "backend")),
        _gr_update(gr, label=ui_text(lang, "optimizer_backend")),
        _gr_update(gr, label=ui_text(lang, "target_backend")),
        _gr_update(gr, label=ui_text(lang, "gate_mode")),
        _gr_update(gr, label=ui_text(lang, "allow_mock")),
        _gr_update(gr, label=ui_text(lang, "resume_run_id"), placeholder=ui_text(lang, "resume_placeholder")),
        _gr_update(gr, value=ui_text(lang, "run_button")),
        ui_text(lang, "run_note"),
        _gr_update(gr, label=ui_text(lang, "review_run_id"), placeholder=ui_text(lang, "review_placeholder")),
        _gr_update(gr, value=ui_text(lang, "review_button")),
        ui_text(lang, "adopt_copy"),
        _gr_update(gr, label=ui_text(lang, "confirmation"), placeholder="Type: ADOPT <run_id>"),
        _gr_update(gr, label=ui_text(lang, "force_guard")),
        _gr_update(gr, value=ui_text(lang, "adopt_button")),
        ui_text(lang, "rollback_copy"),
        _gr_update(gr, label=ui_text(lang, "confirmation"), placeholder="Type: ROLLBACK <run_id>"),
        _gr_update(gr, label=ui_text(lang, "force_guard")),
        _gr_update(gr, value=ui_text(lang, "rollback_button")),
        ui_text(lang, "upstream_copy"),
        _gr_update(gr, label=ui_text(lang, "fetch_only")),
        _gr_update(gr, value=ui_text(lang, "upstream_status")),
        _gr_update(gr, value=ui_text(lang, "parity_status")),
        _gr_update(gr, value=ui_text(lang, "upstream_update")),
    )


def make_code_component(gr: Any, *, label: str, language: str | None = None):
    """Create gr.Code while avoiding language names unsupported by some Gradio versions."""
    if language == "diff":
        language = None
    kwargs: dict[str, Any] = {"label": label}
    if language:
        kwargs["language"] = language
    return gr.Code(**kwargs)


def _supports_kwarg(callable_obj: Any, kwarg: str) -> bool:
    try:
        return kwarg in inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False


def blocks_kwargs(gr: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"title": "Hermes SkillOpt"}
    if _supports_kwarg(gr.Blocks, "css"):
        kwargs["css"] = WEBUI_CSS
    if _supports_kwarg(gr.Blocks, "js"):
        kwargs["js"] = pwa_startup_js()
    if _supports_kwarg(gr.Blocks, "head"):
        kwargs["head"] = pwa_head_html()
    return kwargs


def launch_kwargs(app: Any, *, host: str, port: int, share: bool, browser: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"server_name": host, "server_port": port, "share": share, "inbrowser": browser}
    if _supports_kwarg(app.launch, "css"):
        kwargs["css"] = WEBUI_CSS
    if _supports_kwarg(app.launch, "js"):
        kwargs["js"] = pwa_startup_js()
    if _supports_kwarg(app.launch, "head"):
        kwargs["head"] = pwa_head_html()
    return kwargs


def pwa_response_headers(*, static_asset: bool = False) -> dict[str, str]:
    # Explicit no-store keeps browsers from passively storing private Gradio/state
    # pages; the service worker itself only stores the static allowlist above.
    cache_control = "public, max-age=3600" if static_asset else "no-store"
    return {"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"}


def _resolve_fastapi_app(app: Any) -> Any | None:
    """Return the concrete FastAPI app from a Gradio Blocks/app wrapper."""
    candidates = (app, getattr(app, "server_app", None), getattr(app, "app", None))
    for candidate in candidates:
        if callable(getattr(candidate, "add_api_route", None)):
            return candidate
    return None


def attach_pwa_routes(app: Any) -> bool:
    """Attach safe PWA endpoints to Gradio's concrete FastAPI app when available."""
    fastapi_app = _resolve_fastapi_app(app)
    add_route = getattr(fastapi_app, "add_api_route", None)
    if not callable(add_route):
        return False
    try:
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # type: ignore
    except Exception:
        return False

    def manifest_endpoint():
        return JSONResponse(pwa_manifest(), media_type="application/manifest+json", headers=pwa_response_headers(static_asset=True))

    def sw_endpoint():
        return PlainTextResponse(service_worker_js(), media_type="application/javascript", headers=pwa_response_headers(static_asset=False))

    def offline_endpoint():
        return HTMLResponse(offline_html(), media_type="text/html", headers=pwa_response_headers(static_asset=False))

    def favicon_endpoint():
        return Response(favicon_svg(), media_type="image/svg+xml", headers=pwa_response_headers(static_asset=True))

    def icon_endpoint(size: int):
        return Response(pwa_icon_png(size), media_type="image/png", headers=pwa_response_headers(static_asset=True))

    routes = {
        route.path for route in getattr(fastapi_app, "routes", []) if hasattr(route, "path")
    }
    if "/manifest.webmanifest" not in routes:
        add_route("/manifest.webmanifest", manifest_endpoint, methods=["GET", "HEAD"], include_in_schema=False)
    if "/sw.js" not in routes:
        add_route("/sw.js", sw_endpoint, methods=["GET", "HEAD"], include_in_schema=False)
    if "/offline.html" not in routes:
        add_route("/offline.html", offline_endpoint, methods=["GET", "HEAD"], include_in_schema=False)
    if "/favicon.svg" not in routes:
        add_route("/favicon.svg", favicon_endpoint, methods=["GET", "HEAD"], include_in_schema=False)
    icon_routes = {
        "/icons/skillopt-icon-192.png": 192,
        "/icons/skillopt-icon-512.png": 512,
        "/icons/apple-touch-icon.png": 180,
    }
    for path, size in icon_routes.items():
        if path not in routes:
            add_route(path, lambda size=size: icon_endpoint(size), methods=["GET", "HEAD"], include_in_schema=False)
    setattr(app, "_skillopt_pwa_routes_attached", True)
    return True


def require_gradio():
    try:
        import gradio as gr  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by tests via monkeypatch
        raise RuntimeError(INSTALL_HINT) from exc
    return gr


def _json(data: Any) -> str:
    return core.redact_secrets(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _safe_artifact_path(run_dir: Path, filename: str) -> Path | None:
    """Return a safe fixed artifact path under run_dir, rejecting symlink escapes."""
    if filename not in ALLOWED_ARTIFACTS:
        return None
    if Path(filename).name != filename:
        return None
    base = run_dir.resolve()
    path = base / filename
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if path.is_symlink() or not core._is_relative_to(resolved, base) or not resolved.is_file():
        return None
    return resolved


def _read_artifact_limited(run_dir: Path, filename: str, limit: int = MAX_TEXT_CHARS) -> str:
    path = _safe_artifact_path(run_dir, filename)
    if path is None:
        return ""
    return core.redact_secrets(path.read_text(encoding="utf-8", errors="replace")[:limit])


def _load_artifact_json(run_dir: Path, filename: str) -> Any:
    text = _read_artifact_limited(run_dir, filename)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _rejected_edit_explorer(rejected_text: str, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in rejected_text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            item = {"raw": line[:500]}
        if isinstance(item, dict):
            rows.append(
                {
                    "iteration": item.get("iteration"),
                    "candidate": item.get("candidate_id") or item.get("candidate"),
                    "reason": item.get("reason") or item.get("rationale") or item.get("gate_reason"),
                    "task_id": item.get("task_id"),
                    "score_delta": item.get("score_delta"),
                    "preview": core.redact_secrets(str(item.get("edit") or item.get("raw") or item)[:500]),
                }
            )
        if len(rows) >= limit:
            break
    return rows


def report_summary_data(manifest: dict[str, Any], run_dir: Path | None = None) -> dict[str, Any]:
    """Build a read-only observability/reporting summary for WebUI and tests."""
    checkpoint = _load_artifact_json(run_dir, "checkpoint.json") if run_dir is not None else None
    rejected_text = _read_artifact_limited(run_dir, "rejected_edits.jsonl") if run_dir is not None else ""
    rejected_preview = _rejected_edit_explorer(rejected_text)
    completed = checkpoint.get("completed_stages") if isinstance(checkpoint, dict) else None
    timeline = {
        "run_status": manifest.get("status"),
        "checkpoint_status": checkpoint.get("status") if isinstance(checkpoint, dict) else None,
        "completed_stages": completed or [],
        "created_at": manifest.get("created_at"),
    }
    eligibility = {
        "adoptable": manifest.get("adoptable") is True,
        "accepted_for_adopt": manifest.get("status") in ("staged_best", "accepted", "adopted") and manifest.get("adoptable") is True,
        "reasons": manifest.get("production_eligibility_reasons") or (["eligible"] if manifest.get("adoptable") is True else []),
        "checklist": {
            "staged_best": manifest.get("status") == "staged_best",
            "production_gate_eligible": manifest.get("production_gate_eligible") is True,
            "heldout_test_gate_eligible": manifest.get("test_gate_eligible") is True,
            "review_only": manifest.get("review_only") is True,
        },
    }
    provenance = manifest.get("provenance_fingerprint") or {}
    policy = manifest.get("production_eval_policy") or {}
    gate_policy = manifest.get("gate_policy") or {}
    security = {
        "artifact_integrity": "hashes_recorded" if manifest.get("artifact_sha256") else "legacy_or_missing_hashes",
        "artifact_count": len(manifest.get("artifact_sha256") or {}),
        "provenance_fingerprint": provenance.get("fingerprint_sha256") if isinstance(provenance, dict) else None,
        "production_eval_policy": policy.get("policy_version") if isinstance(policy, dict) else None,
        "optimizer_backend": manifest.get("optimizer_backend") or manifest.get("backend"),
        "target_executor": manifest.get("target_executor"),
        "gate_policy": gate_policy.get("mode") if isinstance(gate_policy, dict) else gate_policy,
        "parity_status": manifest.get("benchmark_parity_status") or {"label": "Hermes-native benchmark mode; no upstream parity claim"},
        "lineage": {"eval_pack": manifest.get("eval_pack"), "eval_pack_governance": manifest.get("eval_pack_governance")},
    }
    return {
        "run_id": manifest.get("run_id"),
        "skill": manifest.get("skill_name"),
        "timeline": timeline,
        "eligibility": eligibility,
        "split_scores": manifest.get("split_scores") or {
            "validation": {"current": manifest.get("validation_current_score"), "candidate": manifest.get("validation_candidate_score")},
            "production_validation": {"current": manifest.get("production_validation_current_score"), "candidate": manifest.get("production_validation_candidate_score")},
            "heldout_test": {"best": manifest.get("test_score")},
        },
        "candidate_comparison": manifest.get("candidate_comparison") or [],
        "regression_cases": manifest.get("regression_cases") or [],
        "provenance_security": security,
        "gate_reason": manifest.get("gate_reason"),
        "rejected_edits": {"count_previewed": len(rejected_preview), "preview": rejected_preview},
    }


def report_summary_markdown(data: dict[str, Any]) -> str:
    eligibility = data.get("eligibility") or {}
    security = data.get("provenance_security") or {}
    timeline = data.get("timeline") or {}
    lines = [
        "## Observability report summary",
        f"- run_id: `{data.get('run_id')}`",
        f"- skill: {data.get('skill')}",
        f"- run_status: {timeline.get('run_status')} (checkpoint={timeline.get('checkpoint_status')})",
        f"- completed_stages: {', '.join(timeline.get('completed_stages') or []) or 'unknown'}",
        f"- adoptable: {eligibility.get('adoptable')}",
        f"- accepted_for_adopt: {eligibility.get('accepted_for_adopt')}",
        f"- not_adoptable_reasons: {eligibility.get('reasons') or []}",
        f"- eligibility_checklist: {json.dumps(eligibility.get('checklist') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- split_scores: {json.dumps(data.get('split_scores') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- candidate_comparison_count: {len(data.get('candidate_comparison') or [])}",
        f"- regression_cases: {data.get('regression_cases') or []}",
        f"- provenance_fingerprint: {security.get('provenance_fingerprint') or 'missing'}",
        f"- production_eval_policy: {security.get('production_eval_policy') or 'missing'}",
        f"- artifact_integrity: {security.get('artifact_integrity')} ({security.get('artifact_count')} files)",
        f"- optimizer/target/gate: {security.get('optimizer_backend')} / {security.get('target_executor')} / {security.get('gate_policy')}",
        f"- benchmark_parity: {json.dumps(security.get('parity_status') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- rejected_edit_preview_count: {(data.get('rejected_edits') or {}).get('count_previewed', 0)}",
    ]
    return "\n".join(lines)


def _run_dir(home: str | None, run_id: str) -> Path:
    return core.resolve_run_dir(core.hermes_home(home), run_id)


def latest_run_id(home: str | None = None) -> str:
    st = core.status(home)
    runs = st.get("recent_runs") or []
    if not runs:
        return ""
    return str(runs[0].get("run_id") or "")


def status_markdown(home: str | None = None) -> str:
    st = core.status(home)
    lines = [
        "## hermes-skillopt status",
        f"- success: {st.get('success')}",
        f"- hermes_home: `{st.get('hermes_home')}`",
        f"- skills_count: {st.get('skills_count')}",
        f"- staging: `{st.get('staging')}`",
        f"- backups: `{st.get('backups')}`",
        "",
        "### Recent staged runs",
    ]
    runs = st.get("recent_runs") or []
    if not runs:
        lines.append("- none")
    for r in runs[:10]:
        reasons = r.get("not_adoptable_reasons") or r.get("production_eligibility_reasons") or []
        split = r.get("split_scores") or {}
        val = split.get("validation") if isinstance(split, dict) else {}
        test = split.get("heldout_test") if isinstance(split, dict) else {}
        lines.append(
            "- `{run_id}` — {status} — adoptable={adoptable} prod_gate={prod} test_gate={test_gate} — {skill} — {engine}{backend} — {created}".format(
                run_id=r.get("run_id") or "",
                status=r.get("status") or "unknown",
                adoptable=r.get("adoptable"),
                prod=r.get("production_gate_eligible"),
                test_gate=r.get("test_gate_eligible"),
                skill=r.get("skill_name") or "unknown-skill",
                engine=r.get("engine") or "unknown-engine",
                backend=("/" + str(r.get("backend"))) if r.get("backend") else "",
                created=r.get("created_at") or "",
            )
        )
        lines.append(f"  - why: {', '.join(map(str, reasons)) if reasons else 'eligible or legacy run'}")
        if isinstance(val, dict) or isinstance(test, dict):
            lines.append(f"  - scores: validation current={val.get('current') if isinstance(val, dict) else None} candidate={val.get('candidate') if isinstance(val, dict) else None}; heldout_test={test.get('best') if isinstance(test, dict) else None}")
    return "\n".join(lines)


def review_payload(run_id: str | None = None, home: str | None = None) -> tuple[str, str, str, str, str, str]:
    rid = (run_id or "").strip() or latest_run_id(home)
    if not rid:
        return "No staged runs found.", "", "", "", "", ""
    try:
        rd = _run_dir(home, rid)
        manifest_text = _read_artifact_limited(rd, "manifest.json")
        if not manifest_text:
            raise ValueError("manifest.json missing or unsafe")
        manifest = json.loads(manifest_text)
        gate_text = _read_artifact_limited(rd, "gate_results.json")
        gate_data = manifest.get("gate")
        if gate_text:
            try:
                gate_data = json.loads(gate_text).get("best_gate")
            except Exception:
                gate_data = gate_text
        report = _read_artifact_limited(rd, "report.md")
        diff = _read_artifact_limited(rd, "diff.patch")
        gate = gate_text or _json(gate_data)
        candidate_summary = _read_artifact_limited(rd, "candidate_summary.json")
        observability = report_summary_data(manifest, rd)
        observability_md = report_summary_markdown(observability)
        if candidate_summary:
            gate = (gate + "\n\n## Candidate summary\n" + candidate_summary) if gate else candidate_summary
        gate = (gate + "\n\n## Exportable observability JSON\n" + _json(observability)) if gate else _json(observability)
        candidate = _read_artifact_limited(rd, "proposed_SKILL.md") or _read_artifact_limited(rd, "best_skill.md")
        rejected = _read_artifact_limited(rd, "rejected_edits.jsonl")
        rejected_preview = _json((observability.get("rejected_edits") or {}).get("preview") or [])
        if rejected_preview != "[]":
            rejected = ("## Rejected edit explorer preview\n" + rejected_preview + "\n\n## Raw rejected_edits.jsonl\n" + rejected) if rejected else rejected_preview
        summary = [
            f"## Review `{rid}`",
            f"- status: {manifest.get('status')}",
            f"- skill: {manifest.get('skill_name')}",
            f"- adoptable: {manifest.get('adoptable')}",
            f"- production_gate_eligible: {manifest.get('production_gate_eligible')}",
            f"- test_gate_eligible: {manifest.get('test_gate_eligible')}",
            f"- not_adoptable_reasons: {manifest.get('production_eligibility_reasons') or []}",
            f"- validation_scores: current={manifest.get('validation_current_score')} candidate={manifest.get('validation_candidate_score')}",
            f"- production_scores: current={manifest.get('production_validation_current_score')} candidate={manifest.get('production_validation_candidate_score')}",
            f"- test_score: {manifest.get('test_score')}",
            f"- evaluator: {manifest.get('target_executor')} / {manifest.get('target_config_id')}",
            f"- accepted_for_adopt: {manifest.get('status') in ('staged_best', 'accepted', 'adopted') and manifest.get('adoptable') is True}",
            f"- run_dir: `{rd}`",
            f"- diff_path: `{rd / 'diff.patch'}`",
            f"- report_path: `{rd / 'report.md'}`",
            "",
            observability_md,
        ]
        return "\n".join(summary), report, diff, gate, candidate, rejected
    except Exception as exc:
        return f"Review failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "", "", "", "", ""


def run_full_callback(
    skill: str | None,
    query: str | None,
    eval_file: str | None,
    lookback_days: int,
    limit: int,
    iterations: int,
    edit_budget: int,
    candidate_count: int,
    backend: str,
    allow_mock: bool,
    home: str | None,
    optimizer_backend: str | None = None,
    target_backend: str | None = None,
    gate_mode: str = "soft",
    resume_run_id: str | None = None,
) -> tuple[str, str, str, str, str, str, str]:
    """Run full cycle, always staged-only from the WebUI."""
    try:
        out = core.full_run(
            skill=skill or None,
            query=query or None,
            eval_file=eval_file or None,
            lookback_days=int(lookback_days),
            limit=int(limit),
            iterations=int(iterations),
            edit_budget=int(edit_budget),
            candidate_count=int(candidate_count),
            backend=backend or "auto",
            optimizer_backend=optimizer_backend or None,
            target_backend=target_backend or None,
            gate_mode=gate_mode or "soft",
            resume_run_id=resume_run_id or None,
            allow_mock=bool(allow_mock),
            auto_adopt=False,
            force=False,
            hermes_home_path=home or None,
        )
        rid = str(out.get("run_id") or "")
        summary = "## Full run complete (staged only)\n\n" + _json(out) + "\n\nNo skill was adopted. Use the Adopt tab with explicit confirmation if desired."
        return (summary, *review_payload(rid, home))
    except Exception as exc:
        return (f"Full run failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}", "", "", "", "", "", "")


def adopt_callback(run_id: str, confirmation: str, force: bool, home: str | None) -> str:
    rid = (run_id or "").strip()
    expected = f"ADOPT {rid}"
    if not rid:
        return "Adopt refused: run_id is required."
    if (confirmation or "").strip() != expected:
        return f"Adopt refused: type `{expected}` exactly to confirm."
    try:
        return "Adopt complete:\n\n```json\n" + _json(core.adopt(rid, hermes_home_path=None, force=bool(force))) + "\n```"
    except Exception as exc:
        return f"Adopt failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def rollback_callback(run_id: str, confirmation: str, force: bool, home: str | None) -> str:
    rid = (run_id or "").strip()
    expected = f"ROLLBACK {rid}"
    if not rid:
        return "Rollback refused: run_id is required."
    if (confirmation or "").strip() != expected:
        return f"Rollback refused: type `{expected}` exactly to confirm."
    try:
        return "Rollback complete:\n\n```json\n" + _json(core.rollback(rid, hermes_home_path=None, force=bool(force))) + "\n```"
    except Exception as exc:
        return f"Rollback failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def upstream_status_markdown(home: str | None = None) -> str:
    try:
        return "```json\n" + _json(core.upstream_status(hermes_home_path=home or None)) + "\n```"
    except Exception as exc:
        return f"Upstream status failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def upstream_update_markdown(home: str | None = None, fetch_only: bool = False) -> str:
    try:
        return "```json\n" + _json(core.upstream_update(hermes_home_path=None, repo_path=None, fetch_only=bool(fetch_only))) + "\n```"
    except Exception as exc:
        return f"Upstream update failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def parity_status_markdown(home: str | None = None) -> str:
    try:
        return "```json\n" + _json(core.benchmark_parity_status(hermes_home_path=home or None)) + "\n```"
    except Exception as exc:
        return f"Benchmark/parity status failed: {type(exc).__name__}: {core.redact_secrets(str(exc))}"


def build_app(home_default: str | None = None):
    gr = require_gradio()
    lang_default = "en"
    with gr.Blocks(**blocks_kwargs(gr)) as app:
        hero = gr.Markdown(hero_markdown(lang_default))
        cards = gr.Markdown(safety_cards_markdown(lang_default))
        with gr.Row(elem_classes=["skillopt-settings-row"]):
            language = gr.Dropdown(list(LANGUAGES.keys()), value=lang_default, label=ui_text(lang_default, "language"), info="English / 中文")
            home = gr.Textbox(label=ui_text(lang_default, "home_label"), info=ui_text(lang_default, "home_info"), placeholder=ui_text(lang_default, "home_placeholder"), value=home_default or "")

        with gr.Tabs():
            with gr.Tab("Status / 状态"):
                status_out = gr.Markdown(value=status_markdown(home_default), elem_classes=["skillopt-status-md"])
                refresh = gr.Button(ui_text(lang_default, "refresh_status"))
                refresh.click(status_markdown, inputs=[home], outputs=[status_out])

            with gr.Tab("Run staged optimization / 运行暂存优化"):
                gr.Markdown(value=ui_text(lang_default, "run_note"), elem_classes=["skillopt-note"])
                with gr.Row():
                    skill = gr.Textbox(label=ui_text(lang_default, "skill_label"), placeholder=ui_text(lang_default, "skill_placeholder"))
                    query = gr.Textbox(label=ui_text(lang_default, "query_label"), placeholder=ui_text(lang_default, "query_placeholder"))
                eval_file = gr.Textbox(label=ui_text(lang_default, "eval_label"), info=ui_text(lang_default, "eval_info"))
                with gr.Row():
                    lookback = gr.Slider(0, 90, value=14, step=1, label=ui_text(lang_default, "lookback"))
                    limit = gr.Slider(1, 200, value=50, step=1, label=ui_text(lang_default, "limit"))
                    iterations = gr.Slider(1, 8, value=1, step=1, label=ui_text(lang_default, "iterations"))
                    edit_budget = gr.Slider(0, 16, value=3, step=1, label=ui_text(lang_default, "edit_budget"))
                    candidate_count = gr.Slider(1, 5, value=1, step=1, label=ui_text(lang_default, "candidate_count"))
                with gr.Row():
                    backend = gr.Dropdown(["auto", "hermes", "mock"], value="auto", label=ui_text(lang_default, "backend"))
                    optimizer_backend = gr.Dropdown(["", "auto", "hermes", "mock"], value="", label=ui_text(lang_default, "optimizer_backend"))
                    target_backend = gr.Dropdown(["", "auto", "replay", "sandbox", "scorecard", "live-readonly"], value="", label=ui_text(lang_default, "target_backend"))
                    gate_mode = gr.Dropdown(["soft", "hard", "mixed", "strict"], value="soft", label=ui_text(lang_default, "gate_mode"))
                    allow_mock = gr.Checkbox(value=False, label=ui_text(lang_default, "allow_mock"))
                resume_run_id = gr.Textbox(label=ui_text(lang_default, "resume_run_id"), placeholder=ui_text(lang_default, "resume_placeholder"))
                run_btn = gr.Button(ui_text(lang_default, "run_button"), variant="primary")
                run_status = gr.Markdown(elem_classes=["skillopt-result-md"])

            with gr.Tab("Review artifacts / 审查产物"):
                review_run_id = gr.Textbox(label=ui_text(lang_default, "review_run_id"), placeholder=ui_text(lang_default, "review_placeholder"))
                review_btn = gr.Button(ui_text(lang_default, "review_button"))
                review_summary = gr.Markdown(elem_classes=["skillopt-result-md"])
                report = gr.Markdown(label="report.md", elem_classes=["skillopt-result-md"])
                diff = make_code_component(gr, label="diff.patch", language="diff")
                gate = make_code_component(gr, label="gate/candidate summary", language="json")
                candidate = make_code_component(gr, label="proposed_SKILL.md", language="markdown")
                rejected = make_code_component(gr, label="rejected_edits.jsonl", language="json")

            with gr.Tab("Adopt / 采用"):
                adopt_copy = gr.Markdown(ui_text(lang_default, "adopt_copy"))
                adopt_run_id = gr.Textbox(label="Run ID")
                adopt_confirm = gr.Textbox(label=ui_text(lang_default, "confirmation"), placeholder="Type: ADOPT <run_id>")
                adopt_force = gr.Checkbox(value=False, label=ui_text(lang_default, "force_guard"))
                adopt_btn = gr.Button(ui_text(lang_default, "adopt_button"), variant="stop")
                adopt_out = gr.Markdown(elem_classes=["skillopt-result-md"])

            with gr.Tab("Rollback / 回滚"):
                rollback_copy = gr.Markdown(ui_text(lang_default, "rollback_copy"))
                rollback_run_id = gr.Textbox(label="Run ID")
                rollback_confirm = gr.Textbox(label=ui_text(lang_default, "confirmation"), placeholder="Type: ROLLBACK <run_id>")
                rollback_force = gr.Checkbox(value=False, label=ui_text(lang_default, "force_guard"))
                rollback_btn = gr.Button(ui_text(lang_default, "rollback_button"), variant="stop")
                rollback_out = gr.Markdown(elem_classes=["skillopt-result-md"])

            with gr.Tab("Upstream / 上游"):
                upstream_copy = gr.Markdown(ui_text(lang_default, "upstream_copy"))
                fetch_only = gr.Checkbox(value=True, label=ui_text(lang_default, "fetch_only"))
                upstream_out = gr.Markdown(elem_classes=["skillopt-result-md"])
                with gr.Row():
                    up_status_btn = gr.Button(ui_text(lang_default, "upstream_status"))
                    parity_btn = gr.Button(ui_text(lang_default, "parity_status"))
                    up_update_btn = gr.Button(ui_text(lang_default, "upstream_update"))

        language_outputs = [
            hero, cards, home, refresh, skill, query, eval_file, lookback, limit, iterations, edit_budget, candidate_count,
            backend, optimizer_backend, target_backend, gate_mode, allow_mock, resume_run_id, run_btn, run_status,
            review_run_id, review_btn, adopt_copy, adopt_confirm, adopt_force, adopt_btn, rollback_copy,
            rollback_confirm, rollback_force, rollback_btn, upstream_copy, fetch_only, up_status_btn, parity_btn, up_update_btn,
        ]
        if hasattr(language, "change"):
            language.change(lambda selected: language_updates(gr, selected), inputs=[language], outputs=language_outputs)
        run_btn.click(
            run_full_callback,
            inputs=[skill, query, eval_file, lookback, limit, iterations, edit_budget, candidate_count, backend, allow_mock, home, optimizer_backend, target_backend, gate_mode, resume_run_id],
            outputs=[run_status, review_summary, report, diff, gate, candidate, rejected],
        )
        review_btn.click(review_payload, inputs=[review_run_id, home], outputs=[review_summary, report, diff, gate, candidate, rejected])
        adopt_btn.click(adopt_callback, inputs=[adopt_run_id, adopt_confirm, adopt_force, home], outputs=[adopt_out])
        rollback_btn.click(rollback_callback, inputs=[rollback_run_id, rollback_confirm, rollback_force, home], outputs=[rollback_out])
        up_status_btn.click(upstream_status_markdown, inputs=[home], outputs=[upstream_out])
        parity_btn.click(parity_status_markdown, inputs=[home], outputs=[upstream_out])
        up_update_btn.click(upstream_update_markdown, inputs=[home, fetch_only], outputs=[upstream_out])
        load = getattr(app, "load", None)
        if callable(load):
            load(fn=None, inputs=None, outputs=None, js=pwa_startup_js())
    attach_pwa_routes(app)
    return app


def launch_webui(app: Any, *, host: str, port: int, share: bool, browser: bool) -> None:
    """Launch Gradio and attach PWA routes to the actual served FastAPI app.

    Blocks.launch() builds/replaces the FastAPI app during launch, so routes
    registered on the pre-launch Blocks object are not necessarily served.
    Launch non-blocking, attach to the concrete server app, then enter Gradio's
    normal blocking loop.
    """
    kwargs = launch_kwargs(app, host=host, port=port, share=share, browser=browser)
    launched_nonblocking = False
    if _supports_kwarg(app.launch, "prevent_thread_lock"):
        kwargs["prevent_thread_lock"] = True
        launched_nonblocking = True
    app.launch(**kwargs)
    attach_pwa_routes(app)
    if launched_nonblocking:
        block_thread = getattr(app, "block_thread", None)
        if callable(block_thread):
            block_thread()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_skillopt.webui", description="Launch the Hermes SkillOpt Gradio WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--browser", action="store_true", help="Open a browser after launch")
    parser.add_argument("--home", help="HERMES_HOME override for WebUI defaults and callbacks")
    args = parser.parse_args(argv)
    try:
        app = build_app(args.home)
    except RuntimeError as exc:
        if str(exc) == INSTALL_HINT:
            print(str(exc), file=sys.stderr)
            return 1
        raise
    launch_webui(app, host=args.host, port=args.port, share=args.share, browser=args.browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

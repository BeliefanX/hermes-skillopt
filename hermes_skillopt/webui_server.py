import argparse
from pathlib import Path
from typing import Any, Optional

from hermes_skillopt import core
from hermes_skillopt import webui as pwa
from hermes_skillopt import webui_api


INSTALL_HINT = (
    "FastAPI and Uvicorn are required for the hermes-skillopt WebUI. Install with: "
    "python3 -m pip install 'hermes-skillopt[webui]' or python3 -m pip install fastapi uvicorn"
)


def _require_fastapi():
    try:
        from fastapi import FastAPI, HTTPException, Query  # type: ignore
        from fastapi.middleware.cors import CORSMiddleware  # type: ignore
        from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # type: ignore
        from fastapi.staticfiles import StaticFiles  # type: ignore
        from pydantic import BaseModel  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(INSTALL_HINT) from exc
    return {
        "FastAPI": FastAPI,
        "HTTPException": HTTPException,
        "Query": Query,
        "CORSMiddleware": CORSMiddleware,
        "HTMLResponse": HTMLResponse,
        "JSONResponse": JSONResponse,
        "PlainTextResponse": PlainTextResponse,
        "Response": Response,
        "StaticFiles": StaticFiles,
        "BaseModel": BaseModel,
    }


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "webui_static"


def create_app(home_default: Any = None):
    deps = _require_fastapi()
    FastAPI = deps["FastAPI"]
    HTTPException = deps["HTTPException"]
    Query = deps["Query"]
    HTMLResponse = deps["HTMLResponse"]
    JSONResponse = deps["JSONResponse"]
    PlainTextResponse = deps["PlainTextResponse"]
    Response = deps["Response"]
    StaticFiles = deps["StaticFiles"]
    BaseModel = deps["BaseModel"]

    def safe_response(fn):
        try:
            return webui_api._safe_json(fn())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=core.redact_secrets(str(exc))) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=core.redact_secrets(str(exc))) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: internal WebUI operation failed") from exc

    class RunRequest(BaseModel):
        intent: str = "review"
        skill: Optional[str] = None
        query: Optional[str] = None
        eval_file: Optional[str] = None
        lookback_days: int = 14
        limit: int = 50
        iterations: int = 1
        edit_budget: int = 3
        candidate_count: int = 1
        backend: str = "auto"
        optimizer_backend: Optional[str] = None
        target_executor: str = "auto"
        target_backend: Optional[str] = None
        gate_mode: str = "soft"
        resume_run_id: Optional[str] = None
        allow_mock: bool = False
        home: Optional[str] = None

    class ConfirmRequest(BaseModel):
        run_id: str
        confirmation: str
        force: bool = False
        home: Optional[str] = None  # accepted for client shape, ignored server-side

    class UpstreamUpdateRequest(BaseModel):
        fetch_only: bool = False
        home: Optional[str] = None  # ignored server-side

    class EvalPackAutopilotRequest(BaseModel):
        skill: str
        output: Optional[str] = None
        write_draft: bool = False
        overwrite: bool = False
        home: Optional[str] = None

    class EvalPackPromoteRequest(BaseModel):
        skill: str
        input_path: str
        output: Optional[str] = None
        overwrite: bool = False
        production: bool = False
        home: Optional[str] = None

    app = FastAPI(title="Hermes SkillOpt WebUI", version="0.1.0")

    app.add_middleware(
        deps["CORSMiddleware"],
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    @app.get("/api/status")
    def api_status(home: Optional[str] = None):
        return safe_response(lambda: webui_api.status(home or home_default))

    @app.get("/api/doctor")
    def api_doctor(home: Optional[str] = None, skill: Optional[str] = None):
        return safe_response(lambda: webui_api.doctor(home or home_default, skill=skill))

    @app.get("/api/eval-pack/doctor")
    def api_eval_pack_doctor(home: Optional[str] = None, skill: Optional[str] = None):
        return safe_response(lambda: webui_api.eval_pack_doctor(home or home_default, skill=skill))

    @app.post("/api/eval-pack/autopilot")
    def api_eval_pack_autopilot(req: EvalPackAutopilotRequest):
        data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        if not data.get("home") and home_default:
            data["home"] = str(home_default)
        return safe_response(lambda: webui_api.eval_pack_autopilot(data))

    @app.post("/api/eval-pack/promote")
    def api_eval_pack_promote(req: EvalPackPromoteRequest):
        data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        if not data.get("home") and home_default:
            data["home"] = str(home_default)
        return safe_response(lambda: webui_api.eval_pack_promote(data))

    @app.post("/api/run")
    def api_run(req: RunRequest):
        data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        if not data.get("home") and home_default:
            data["home"] = str(home_default)
        return safe_response(lambda: webui_api.run_full(data))

    @app.get("/api/review")
    def api_review(run_id: Optional[str] = "", home: Optional[str] = None):
        return safe_response(lambda: webui_api.review(run_id, home or home_default))

    @app.get("/api/review/latest")
    def api_review_latest(home: Optional[str] = None):
        return safe_response(lambda: webui_api.review_latest(home or home_default))

    @app.get("/api/review/summary")
    def api_review_summary(run_id: Optional[str] = "", home: Optional[str] = None):
        return safe_response(lambda: core.review_decision_summary(run_id or "latest", hermes_home_path=home or home_default))

    @app.get("/api/fleet/report")
    def api_fleet_report(home: Optional[str] = None, limit: int = Query(50, ge=1, le=200), skill: Optional[str] = None):
        return safe_response(lambda: webui_api.fleet_report(home or home_default, limit=limit, skill=skill))

    @app.get("/api/fleet/resume-plan")
    def api_fleet_resume_plan(home: Optional[str] = None, limit: int = Query(50, ge=1, le=200), skill: Optional[str] = None):
        return safe_response(lambda: webui_api.fleet_resume_plan(home or home_default, limit=limit, skill=skill))

    @app.get("/api/fleet/rollback-plan")
    def api_fleet_rollback_plan(home: Optional[str] = None, limit: int = Query(50, ge=1, le=200), skill: Optional[str] = None):
        return safe_response(lambda: webui_api.fleet_rollback_plan(home or home_default, limit=limit, skill=skill))

    @app.post("/api/adopt")
    def api_adopt(req: ConfirmRequest):
        return safe_response(lambda: webui_api.adopt(req.run_id, req.confirmation, req.force))

    @app.post("/api/rollback")
    def api_rollback(req: ConfirmRequest):
        return safe_response(lambda: webui_api.rollback(req.run_id, req.confirmation, req.force))

    @app.get("/api/upstream/status")
    def api_upstream_status(home: Optional[str] = None):
        return safe_response(lambda: webui_api.upstream_status(home or home_default))

    @app.get("/api/upstream/parity")
    def api_upstream_parity(home: Optional[str] = None):
        return safe_response(lambda: webui_api.upstream_parity(home or home_default))

    @app.post("/api/upstream/update")
    def api_upstream_update(req: UpstreamUpdateRequest):
        return safe_response(lambda: webui_api.upstream_update(req.fetch_only))

    @app.get("/manifest.json", include_in_schema=False)
    def manifest_json_alias():
        return JSONResponse(pwa.pwa_manifest(), media_type="application/manifest+json", headers=pwa.pwa_response_headers(static_asset=True))

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return JSONResponse(pwa.pwa_manifest(), media_type="application/manifest+json", headers=pwa.pwa_response_headers(static_asset=True))

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return PlainTextResponse(pwa.service_worker_js(), media_type="application/javascript", headers=pwa.pwa_response_headers(static_asset=False))

    @app.get("/offline.html", include_in_schema=False)
    def offline():
        return HTMLResponse(pwa.offline_html(), headers=pwa.pwa_response_headers(static_asset=False))

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return Response(pwa.favicon_svg(), media_type="image/svg+xml", headers=pwa.pwa_response_headers(static_asset=True))

    @app.get("/icons/{name}", include_in_schema=False)
    def icons(name: str):
        sizes = {"skillopt-icon-192.png": 192, "skillopt-icon-512.png": 512, "apple-touch-icon.png": 180}
        if name not in sizes:
            raise HTTPException(status_code=404, detail="icon not found")
        return Response(pwa.pwa_icon_png(sizes[name]), media_type="image/png", headers=pwa.pwa_response_headers(static_asset=True))

    assets = static_dir()
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str = ""):
        index = assets / "index.html"
        if not index.exists():
            fallback = "<h1>Hermes SkillOpt WebUI assets missing</h1><p>Run npm install && npm run build in web/.</p>"
            return HTMLResponse(fallback, status_code=503)
        return HTMLResponse(index.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return app


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_skillopt.webui", description="Launch the Hermes SkillOpt React/FastAPI WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="Accepted for CLI compatibility; FastAPI server stays local unless host is changed")
    parser.add_argument("--browser", action="store_true", help="Open a browser after launch")
    parser.add_argument("--home", help="HERMES_HOME override for read/run/review/status defaults")
    args = parser.parse_args(argv)
    try:
        import uvicorn  # type: ignore
    except ImportError as exc:  # pragma: no cover
        print(INSTALL_HINT)
        raise SystemExit(1) from exc
    if args.browser:
        import webbrowser

        webbrowser.open(f"http://{args.host}:{args.port}/")
    uvicorn.run(create_app(args.home), host=args.host, port=args.port)
    return 0

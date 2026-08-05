from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from archeon.claims.schema import StaleClaimError, save_claim
from archeon.db import connect_readonly
from archeon.review import render, store


class ReviewIn(BaseModel):
    status: str
    version: str
    statement: str | None = None


def create_app(claims_dir, db=None) -> FastAPI:
    claims_dir = Path(claims_dir)
    app = FastAPI(title="Archeon Review")

    @contextmanager
    def db_conn():
        # A fresh read-only connection per request: FastAPI runs these sync
        # routes in a threadpool, and a sqlite3 connection may only be used in
        # the thread that created it. None when no DB was configured.
        conn = connect_readonly(db) if db else None
        try:
            yield conn
        finally:
            if conn is not None:
                conn.close()

    @app.get("/api/components")
    def components():
        return store.components(claims_dir)

    @app.get("/api/clusters")
    def clusters(component: str):
        with db_conn() as conn:
            return store.clusters(claims_dir, component, conn=conn)

    @app.get("/api/claims")
    def claims(component: str | None = None, cluster: str | None = None):
        with db_conn() as conn:
            cards = store.claims_in(claims_dir, component=component,
                                    cluster=cluster, conn=conn)
        for c in cards:
            if not c.get("broken"):
                c["render"] = render.render_spec(c)
        return cards

    @app.get("/api/queue")
    def review_queue():
        with db_conn() as conn:
            cards = store.queue(claims_dir, conn=conn)
        for c in cards:
            c["render"] = render.render_spec(c)
        return cards

    @app.post("/api/claims/{claim_id}")
    def review(claim_id: str, body: ReviewIn):
        try:
            version = save_claim(claims_dir, claim_id, status=body.status,
                                 statement=body.statement,
                                 expected_version=body.version)
        except StaleClaimError:
            raise HTTPException(status_code=409,
                                detail="claim changed on disk; reload")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="claim not found")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"write failed: {e}")
        return {"ok": True, "version": version}

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from fake_servers.providers.base import (
    JiraDataProvider, ProviderError, ProviderNotFoundError,
)


def create_jira_app(provider: JiraDataProvider) -> FastAPI:
    app = FastAPI(title="Fake Jira Server")

    @app.get("/rest/api/2/issue/{issue_key}")
    async def get_issue(issue_key: str):
        try:
            issue = await provider.get_issue(issue_key)
            return JSONResponse({
                "id": issue.key,
                "key": issue.key,
                "fields": {
                    "summary": issue.summary,
                    "description": issue.description,
                    "issuetype": {"name": issue.issuetype},
                    "status": {"name": issue.status},
                    "labels": issue.labels,
                },
            })
        except ProviderNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/rest/api/2/issue/{issue_key}/comment")
    async def get_comments(issue_key: str):
        try:
            comments = await provider.get_comments(issue_key)
            return JSONResponse({
                "comments": [
                    {
                        "id": str(c.id),
                        "body": c.body,
                        "author": {"displayName": c.author},
                        "created": c.created.isoformat(),
                    }
                    for c in comments
                ],
                "total": len(comments),
            })
        except ProviderNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.post("/_benchmark/reset")
    async def reset():
        return JSONResponse({"status": "ok"})

    return app

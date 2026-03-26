from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from fake_servers.providers.base import (
    BitbucketDataProvider, BitbucketWriteSink,
    CommentAnchor, ProviderError, ProviderNotFoundError,
)


def create_bitbucket_app(
    provider: BitbucketDataProvider,
    write_sink: BitbucketWriteSink,
) -> FastAPI:
    app = FastAPI(title="Fake Bitbucket Server")

    def _pr_to_dict(pr) -> dict:
        return {
            "id": pr.id,
            "title": pr.title,
            "description": pr.description,
            "state": pr.status,
            "fromRef": {
                "displayId": pr.from_branch,
                "latestCommit": pr.head_commit,
            },
            "toRef": {
                "displayId": pr.to_branch,
            },
            "author": {
                "user": {
                    "name": pr.author.name,
                    "displayName": pr.author.display_name,
                }
            },
        }

    def _diff_to_dict(diffs) -> dict:
        files = []
        for fd in diffs:
            hunks = []
            for h in fd.hunks:
                segments = []
                added_lines, removed_lines, context_lines = [], [], []
                for line in h.lines:
                    if line.startswith("+"):
                        added_lines.append(line[1:])
                    elif line.startswith("-"):
                        removed_lines.append(line[1:])
                    else:
                        context_lines.append(line[1:] if line.startswith(" ") else line)

                # Build segments
                if context_lines:
                    segments.append({
                        "type": "CONTEXT",
                        "lines": [{"line": l, "truncated": False} for l in context_lines],
                    })
                if removed_lines:
                    segments.append({
                        "type": "REMOVED",
                        "lines": [{"line": l, "truncated": False} for l in removed_lines],
                    })
                if added_lines:
                    segments.append({
                        "type": "ADDED",
                        "lines": [{"line": l, "truncated": False} for l in added_lines],
                    })

                hunks.append({
                    "sourceLine": h.old_start,
                    "destinationLine": h.new_start,
                    "segments": segments,
                })

            files.append({
                "source": {"toString": fd.path},
                "destination": {"toString": fd.path},
                "hunks": hunks,
                "fileType": fd.change_type,
            })
        return {"diffs": files}

    @app.get("/rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{pr_id}")
    async def get_pull_request(proj: str, repo: str, pr_id: int):
        try:
            pr = await provider.get_pull_request(proj, repo, pr_id)
            return JSONResponse(_pr_to_dict(pr))
        except ProviderNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/diff")
    async def get_diff(proj: str, repo: str, pr_id: int):
        try:
            diffs = await provider.get_diff(proj, repo, pr_id)
            return JSONResponse(_diff_to_dict(diffs))
        except ProviderNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/rest/api/1.0/projects/{proj}/repos/{repo}/browse/{path:path}")
    async def get_file(proj: str, repo: str, path: str, at: str = "HEAD"):
        try:
            file = await provider.get_file(proj, repo, path, at)
            if file is None:
                raise HTTPException(status_code=404, detail=f"File not found: {path}")
            lines = file.content.splitlines()
            return JSONResponse({
                "lines": [{"text": line} for line in lines],
                "size": len(file.content),
                "isLastPage": True,
            })
        except HTTPException:
            raise
        except ProviderNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.post("/rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/comments")
    async def add_comment(proj: str, repo: str, pr_id: int, request: Request):
        body = await request.json()
        text = body.get("text", "")
        anchor_data = body.get("anchor")
        anchor = None
        if anchor_data:
            anchor = CommentAnchor(
                path=anchor_data.get("path", ""),
                line=anchor_data.get("line", 0),
                line_type=anchor_data.get("lineType", "ADDED"),
            )
        thread = await write_sink.add_comment(pr_id, text, anchor)
        return JSONResponse(
            {
                "id": thread.id,
                "text": thread.text,
                "severity": thread.severity,
                "anchor": {
                    "path": anchor.path,
                    "line": anchor.line,
                    "lineType": anchor.line_type,
                } if anchor else None,
            },
            status_code=201,
        )

    @app.post("/rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/participants")
    async def set_participant(proj: str, repo: str, pr_id: int, request: Request):
        body = await request.json()
        status = body.get("status", "")
        rs = await write_sink.set_review_status(pr_id, status)
        return JSONResponse({"status": rs.status, "approved": status == "APPROVED"})

    @app.get("/_benchmark/captured")
    async def get_captured():
        captured = await write_sink.get_captured()
        comments = []
        for c in captured.comments:
            comments.append({
                "id": c.id,
                "text": c.text,
                "severity": c.severity,
                "anchor": {
                    "path": c.anchor.path,
                    "line": c.anchor.line,
                    "lineType": c.anchor.line_type,
                } if c.anchor else None,
                "created_at": c.created_at.isoformat(),
            })
        return JSONResponse({
            "comments": comments,
            "review_status": {
                "status": captured.review_status.status,
                "set_at": captured.review_status.set_at.isoformat(),
            } if captured.review_status else None,
        })

    @app.post("/_benchmark/reset")
    async def reset():
        await write_sink.reset()
        return JSONResponse({"status": "ok"})

    return app

"""Run the trace server: python -m tracing.server"""
import os
import uvicorn

from .app import app


def main():
    host = os.environ.get("TRACE_HOST", "0.0.0.0")
    port = int(os.environ.get("TRACE_PORT", "8080"))
    log_level = os.environ.get("TRACE_LOG_LEVEL", "info")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()

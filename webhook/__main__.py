"""
Run the webhook server: python -m webhook --config webhook.toml
"""
import argparse
import logging
import uvicorn

from .server import init_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="DiffGraph Webhook Router")
    parser.add_argument("--config", default="webhook.toml", help="TOML config file")
    parser.add_argument("--port", type=int, default=None, help="Override port")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    args = parser.parse_args()

    app = init_app(args.config)

    from .config import load_config
    cfg = load_config(args.config)
    port = args.port or cfg.port

    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()

import os

token: str = os.environ.get("BOT_TOKEN")

logs: int | None = None
max_filesize: int = 50000000
output_folder: str = "/tmp/satoru"

allowed_domains: list[str] = ["*"]
secret_key: str = os.environ.get("SECRET_KEY", "any-secret-you-like")
js_runtime: dict | None = {"node": {"path": "node"}}

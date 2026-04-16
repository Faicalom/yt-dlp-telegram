import os

token: str = os.environ.get("8576952079:AAEPXsaVcLX5oXK0T6VUX8Jzgi3mlxIXRwo")

logs: int | None = None
max_filesize: int = 50000000
output_folder: str = "/tmp/satoru"

allowed_domains: list[str] = ["*"]
secret_key: str = "any-secret-you-like"
js_runtime: dict | None = {"node": {"path": "node"}}

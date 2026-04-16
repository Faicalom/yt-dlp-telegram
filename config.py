token: str = "هنا حط توكن البوت اللي من BotFather"

logs: int | None = None                    # (اختياري) ID قناة اللوج
max_filesize: int = 50000000               # 50 ميجا (Telegram limit)
output_folder: str = "/tmp/satoru"

allowed_domains: list[str] = [ ... ]       # (اتركه زي ما هو)
secret_key: str = "اي-سر-تحبه"
js_runtime: dict | None = {"node": {"path": "node"}}

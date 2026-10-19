"""Stable error categories, including historical provider messages."""
ERROR_LABELS = {"all": "全部错误类型", "target": "目标不可回复", "quota": "账号额度限制", "other": "其他错误"}
TARGET_PATTERNS = ("(385)", "(433)", "Tweet that is deleted or not visible", "restricted who can reply", "目标不存在或不可用")
QUOTA_PATTERNS = ("(501)", "hit the daily limit")

def error_type(message):
    text = (message or "").lower()
    for kind, patterns in (("quota", QUOTA_PATTERNS), ("target", TARGET_PATTERNS)):
        if any(p.lower() in text for p in patterns):
            return kind
    return "other"

def error_filter(kind, column="error_msg"):
    if kind not in ERROR_LABELS:
        raise ValueError("Invalid error type")
    if kind == "all":
        return "1=1", []
    def clause(patterns):
        return "(" + " OR ".join(f"{column} LIKE ?" for _ in patterns) + ")"
    if kind == "target":
        return clause(TARGET_PATTERNS), [f"%{p}%" for p in TARGET_PATTERNS]
    if kind == "quota":
        return clause(QUOTA_PATTERNS), [f"%{p}%" for p in QUOTA_PATTERNS]
    patterns = TARGET_PATTERNS + QUOTA_PATTERNS
    return f"COALESCE({column}, '') <> '' AND NOT " + clause(patterns), [f"%{p}%" for p in patterns]

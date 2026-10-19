"""Cancellation checkpoints for a deleted search rule."""
from contextvars import ContextVar
from ..db.database import get_conn

active_rule = ContextVar("active_search_rule", default=None)

class SearchCancelled(Exception):
    pass

def check_rule(rule_id=None, conn=None):
    rule_id = active_rule.get() if rule_id is None else rule_id
    if rule_id is None:
        return
    if conn is None:
        with get_conn() as connection:
            return check_rule(rule_id, connection)
    if conn.execute("SELECT 1 FROM search_rules WHERE id=?", (rule_id,)).fetchone() is None:
        raise SearchCancelled("搜索规则已删除，停止后续处理")

"""Store check: closed postings leave, outages do not empty the list. Run: python test_sweep.py"""
import sweep as op


def item(n, source="Acme", deadline=""):
    return {"id": f"id{n}", "title": f"Role {n}", "link": f"https://x.example/{n}", "source": source,
            "location": "Austin, TX", "deadline": deadline, "posted": "2026-11-01", "tn": False}


store = {}
assert op.merge_store(store, [item(1), item(2), item(3, deadline="2026-11-30")], {"Acme"}, "2026-11-25") == []
closed = op.merge_store(store, [item(1)], {"Acme"}, "2026-12-01")
assert sorted(c["id"] for c in closed) == ["id2", "id3"], closed
assert store["items"]["https://x.example/1"]["first_seen"] == "2026-11-25"
assert op.merge_store(store, [], set(), "2026-12-02") == [] and len(store["items"]) == 1
print("ok")

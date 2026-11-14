"""Restore Monday and seed memory atomically, without dropping any collection."""

from app import clock
from app.demo.baseline import load_baseline
from app.demo.state import operation_lock
from app.demo.warmup import DATASET, run_warmup
from app.policy import store
from app.query import executor


def reset_demo(db):
    """Return Monday only after employee restore and both warmup releases commit.

    Prerequisites are checked before clearing anything. The audit ledger is append-only;
    kill_switch is retained and deactivated, while only review approvals are removed.
    """
    with operation_lock:
        baseline = {doc["EmployeeNumber"]: doc for doc in load_baseline()}
        store.clear_cache()
        try:
            store.load_active(db, DATASET)
        except ValueError as exc:
            raise LookupError("an active v2 policy is required; run scripts/seed_policies.py") from exc

        def reset_transaction(session):
            at = clock.reset(db=db, session=session)
            current = {doc["EmployeeNumber"]: doc for doc in
                       db.employees.find({}, {"_id": 0}, session=session)}
            if current.keys() != baseline.keys():
                raise LookupError("employees do not match the baseline; run scripts/load_data.py first")
            for number, original in baseline.items():
                if current[number] != original:
                    result = db.employees.replace_one({"EmployeeNumber": number}, original,
                                                      session=session)
                    if result.matched_count != 1:
                        raise LookupError(f"employee {number} disappeared during reset")
            db.releases.delete_many({}, session=session)
            db.precedents.delete_many({}, session=session)
            db.approvals.delete_many({"kind": "review"}, session=session)
            db.approvals.update_one({"_id": "kill_switch"}, {"$set": {"active": False}},
                                    session=session)
            # Keep the append-only audit, but start a new L5 counting interval.
            # Use the same ObjectId ordering as reviewer reset_after in main.
            latest = db.ledger.find_one({}, {"_id": 1}, sort=[("_id", -1)], session=session)
            db.meta.update_one({"_id": "demo_reset"},
                               {"$set": {"ledger_after": latest["_id"] if latest else None}},
                               upsert=True, session=session)
            executor.clear_cache()
            run_warmup(db, session=session)
            return at

        try:
            with db.client.start_session() as session:
                return session.with_transaction(reset_transaction)
        finally:
            # An aborted transaction must not leave field types cached from its snapshot.
            executor.clear_cache()
            store.clear_cache()

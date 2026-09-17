from promo_stack_v11 import PromoLayer, PromoContext, evaluate_stack

# Sequential confirmed stack with cap.
r = evaluate_stack(1000, [
    PromoLayer(kind="product_percent", label="Store 20%", percent=20, verified=True, stackable=True, order=10),
    PromoLayer(kind="coupon_fixed", label="Coupon 100", value=100, verified=True, stackable=True, order=20),
])
assert r["final_price"] == 700.0, r
assert r["effective_discount_percent"] == 30.0, r

# Unknown stackability must not be blindly combined.
r = evaluate_stack(1000, [
    PromoLayer(kind="product_percent", percent=20, verified=True),
    PromoLayer(kind="coupon_fixed", value=100, verified=True),
])
assert r["final_price"] in (800.0, 900.0), r
assert len(r["applied"]) == 1, r

# Bank promo requires matching payment method.
r = evaluate_stack(1000, [
    PromoLayer(kind="bank_percent", percent=15, verified=True, payment_method="visa"),
], PromoContext(payment_method=None))
assert r["final_price"] == 1000.0, r
r = evaluate_stack(1000, [
    PromoLayer(kind="bank_percent", percent=15, verified=True, payment_method="visa"),
], PromoContext(payment_method="visa"))
assert r["final_price"] == 850.0, r

# Checkout-confirmed price wins.
r = evaluate_stack(1000, [], PromoContext(checkout_price=610, checkout_confirmed=True))
assert r["final_price"] == 610.0 and r["confidence"] == "checkout_confirmed", r
print("PASS promo_stack_v11 offline tests")

from promo_stack_v11 import evaluate_legacy_amazon
r = evaluate_legacy_amazon(1000, [
    {"type":"product_percent","percent":20,"verified":True,"label":"20%"},
    {"type":"coupon_fixed","value":200,"verified":True,"label":"coupon"},
    {"type":"bank_percent","percent":15,"verified":True,"label":"visa"},
])
# Legacy evidence does not prove stackability/payment eligibility, so it must
# not claim 55% by blindly adding/stacking the three promos.
assert r["effective_discount_percent"] == 20.0, r
print("PASS legacy Amazon promo safety test")

import os, tempfile
import v11_state
from types import SimpleNamespace
with tempfile.TemporaryDirectory() as td:
    v11_state.DB_PATH = os.path.join(td, "state.db")
    v11_state._RESTOCK_FLAGS.clear()
    d = SimpleNamespace(store="test", title="Priority Product", current_price=100, old_price=200, url="https://example.com/p")
    fp = "a" * 64
    assert v11_state.observe_before_save(fp, d) is False
    v11_state.record_priority(fp, {"class":"ULTRA","route":"PRIVATE_PRIORITY"})
    # Force an old last_seen without sleeping.
    import sqlite3, time
    con = sqlite3.connect(v11_state.DB_PATH)
    con.execute("UPDATE product_presence SET last_seen=? WHERE fingerprint=?", (time.time()-50000, fp)); con.commit(); con.close()
    assert v11_state.observe_before_save(fp, d) is True
    v11_state.record_event(fp, "private_sent", d, {"ok":True})
    s = v11_state.summary()
    assert s["restock_alerts"] == 1 and s["event_counts"].get("private_sent") == 1, s
print("PASS restock + benchmark state tests")

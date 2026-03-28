#!/usr/bin/env python3
"""
test_exchange.py — Testuje czy Bitget API poprawnie obsługuje TPSL na częściach pozycji

Sekwencja testów:
  [1] Pobierz aktualną cenę SOL
  [2] Otwórz pozycję: market order, 1.0 SOL long
  [3] Złóż TP1:  profit_plan, 0.5 SOL, trigger = cena + 3%
  [4] Złóż TP2:  profit_plan, 0.5 SOL, trigger = cena + 6%
  [5] Złóż SL:   loss_plan,   1.0 SOL, trigger = cena - 3%
  [6] Sprawdź czy wszystkie 3 ordery widoczne w API (pending list)
  [7] Zmodyfikuj SL: nowa cena = cena + 0.5%, nowy size = 0.5 SOL (symulacja po TP1)
  [8] Sprawdź czy modyfikacja się przyjęła
  [9] Podsumowanie — co zadziałało, co nie

Uruchomienie:
  python test_exchange.py
  python test_exchange.py --close    # zamknij pozycję po testach
  python test_exchange.py --dry-run  # tylko wypisz co by zostało wysłane (nie wysyła)

Zmienne środowiskowe:
  BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
  BITGET_DEMO (domyślnie "true")
"""

import os, sys, json, time, hmac, hashlib, base64, math, argparse
import requests

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL       = "SOLUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN  = "USDT"
MARGIN_MODE  = "crossed"
LEVERAGE     = 20
BASE_URL     = "https://api.bitget.com"
TEST_QTY     = 1.0   # SOL — rozmiar testowej pozycji
HALF_QTY     = 0.5   # SOL — połowa


# ── Klient API (skopiowany z exchange_trader.py) ───────────────────────────────

class BitgetClient:
    def __init__(self, key, secret, passphrase, demo=True):
        self.key, self.secret, self.passphrase, self.demo = key, secret, passphrase, demo

    def _sign(self, ts, method, path, body):
        msg = ts + method.upper() + path + (body or "")
        mac = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method, path, body=""):
        ts = str(int(time.time() * 1000))
        h = {
            "ACCESS-KEY":        self.key,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }
        if self.demo:
            h["paptrading"] = "1"
        return h

    def post(self, path, params, dry_run=False):
        body = json.dumps(params)
        if dry_run:
            print(f"  [DRY-RUN] POST {path}")
            print(f"  {json.dumps(params, indent=4)}")
            return {"code": "DRY_RUN", "data": {"orderId": f"dry_{int(time.time())}"}}
        resp = requests.post(BASE_URL + path,
                             headers=self._headers("POST", path, body),
                             data=body, timeout=10)
        return resp.json()

    def get(self, path, params=None):
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = requests.get(BASE_URL + path + qs,
                            headers=self._headers("GET", path + qs),
                            timeout=10)
        return resp.json()


# ── Helpers wypisywania ────────────────────────────────────────────────────────

def ok(msg):   print(f"  ✅  {msg}")
def fail(msg): print(f"  ❌  {msg}")
def info(msg): print(f"  ℹ️   {msg}")
def raw(label, data):
    print(f"  RAW {label}: {json.dumps(data, ensure_ascii=False)}")

def sep(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Testy ──────────────────────────────────────────────────────────────────────

def test_get_price(client):
    sep("[1] Pobierz aktualną cenę SOL")
    resp = client.get("/api/v2/mix/market/ticker", {
        "symbol":      SYMBOL,
        "productType": PRODUCT_TYPE,
    })
    raw("ticker", resp)
    if resp.get("code") == "00000":
        price = float(resp["data"][0]["lastPr"])
        ok(f"Cena SOL: ${price:.2f}")
        return price
    else:
        fail(f"Nie udało się pobrać ceny: {resp.get('msg')}")
        return None


def test_open_position(client, price, dry_run=False):
    sep("[2] Otwórz pozycję: market long, 1.0 SOL")
    params = {
        "symbol":      SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginMode":  MARGIN_MODE,
        "marginCoin":  MARGIN_COIN,
        "size":        "1",
        "orderType":   "market",
        "side":        "buy",
        "tradeSide":   "open",
        "posSide":     "long",
    }
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/place-order", params, dry_run=dry_run)
    raw("place-order", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        oid = resp.get("data", {}).get("orderId", "?")
        ok(f"Pozycja otwarta, orderId={oid}")
        return oid
    else:
        fail(f"Błąd otwarcia pozycji: code={resp.get('code')} msg={resp.get('msg')}")
        return None


def test_place_tp1(client, price, dry_run=False):
    sep("[3] Złóż TP1: profit_plan, 0.5 SOL, trigger = cena + 3%")
    tp1_price = round(price * 1.03, 2)
    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginCoin":   MARGIN_COIN,
        "planType":     "profit_plan",
        "triggerPrice": f"{tp1_price:.2f}",
        "triggerType":  "mark_price",
        "executePrice": "0",
        "holdSide":     "long",
        "size":         "0.5",
    }
    info(f"TP1 trigger: ${tp1_price:.2f} (cena + 3%)")
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/place-tpsl-order", params, dry_run=dry_run)
    raw("place-tpsl (TP1)", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        oid = resp.get("data", {}).get("orderId", "?")
        ok(f"TP1 złożony, orderId={oid}")
        return oid, tp1_price
    else:
        fail(f"Błąd TP1: code={resp.get('code')} msg={resp.get('msg')}")
        return None, tp1_price


def test_place_tp2(client, price, dry_run=False):
    sep("[4] Złóż TP2: profit_plan, 0.5 SOL, trigger = cena + 6%")
    tp2_price = round(price * 1.06, 2)
    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginCoin":   MARGIN_COIN,
        "planType":     "profit_plan",
        "triggerPrice": f"{tp2_price:.2f}",
        "triggerType":  "mark_price",
        "executePrice": "0",
        "holdSide":     "long",
        "size":         "0.5",
    }
    info(f"TP2 trigger: ${tp2_price:.2f} (cena + 6%)")
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/place-tpsl-order", params, dry_run=dry_run)
    raw("place-tpsl (TP2)", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        oid = resp.get("data", {}).get("orderId", "?")
        ok(f"TP2 złożony, orderId={oid}")
        return oid, tp2_price
    else:
        fail(f"Błąd TP2: code={resp.get('code')} msg={resp.get('msg')}")
        return None, tp2_price


def test_place_sl(client, price, dry_run=False):
    sep("[5] Złóż SL: loss_plan, 1.0 SOL, trigger = cena - 3%")
    sl_price = round(price * 0.97, 2)
    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginCoin":   MARGIN_COIN,
        "planType":     "loss_plan",
        "triggerPrice": f"{sl_price:.2f}",
        "triggerType":  "mark_price",
        "executePrice": "0",
        "holdSide":     "long",
        "size":         "1.0",
    }
    info(f"SL trigger: ${sl_price:.2f} (cena - 3%)")
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/place-tpsl-order", params, dry_run=dry_run)
    raw("place-tpsl (SL)", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        oid = resp.get("data", {}).get("orderId", "?")
        ok(f"SL złożony, orderId={oid}")
        return oid, sl_price
    else:
        fail(f"Błąd SL: code={resp.get('code')} msg={resp.get('msg')}")
        return None, sl_price


def test_verify_tpsl_pending(client, tp1_oid, tp2_oid, sl_oid):
    sep("[6] Sprawdź pending TPSL orders")

    for plan_type, label, expected_oid in [
        ("profit_plan", "TP (profit_plan)", tp1_oid),
        ("loss_plan",   "SL (loss_plan)",   sl_oid),
    ]:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    plan_type,
        })
        raw(f"pending {plan_type}", resp)
        if resp.get("code") == "00000":
            orders = resp["data"].get("entrustedList", [])
            ids    = [o["orderId"] for o in orders]
            info(f"{label}: znaleziono {len(orders)} orderów, IDs: {ids}")

            if tp1_oid in ids:
                ok(f"TP1 ({tp1_oid}) widoczny w pending ✓")
            elif tp1_oid:
                fail(f"TP1 ({tp1_oid}) NIE widoczny w pending")

            if plan_type == "profit_plan" and tp2_oid in ids:
                ok(f"TP2 ({tp2_oid}) widoczny w pending ✓")
            elif plan_type == "profit_plan" and tp2_oid:
                fail(f"TP2 ({tp2_oid}) NIE widoczny w pending")

            if plan_type == "loss_plan" and sl_oid in ids:
                ok(f"SL  ({sl_oid}) widoczny w pending ✓")
            elif plan_type == "loss_plan" and sl_oid:
                fail(f"SL  ({sl_oid}) NIE widoczny w pending")
        else:
            fail(f"Błąd odpytania pending {plan_type}: {resp.get('msg')}")


def test_modify_sl(client, sl_oid, price, dry_run=False):
    sep("[7] Zmodyfikuj SL: nowa cena = cena - 1.5%, nowy size = 0.5 SOL")
    if not sl_oid:
        fail("Brak sl_oid — pomijam modyfikację")
        return False

    # SL dla long MUSI być poniżej aktualnej ceny.
    # Symulujemy SLpoTP1 jako -1.5% od aktualnej ceny (zamiast -3% jak oryginał).
    # W realu: gdy TP1 odpali przy cenie wyższej niż entry, SLpoTP1 = breakeven < mark_price.
    new_sl_price = round(price * 0.985, 2)
    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginCoin":   MARGIN_COIN,
        "orderId":      sl_oid,
        "triggerPrice": f"{new_sl_price:.2f}",
        "triggerType":  "mark_price",
        "size":         "0.5",
    }
    info(f"Modyfikacja SL {sl_oid}:")
    info(f"  triggerPrice: ${new_sl_price:.2f} (cena - 1.5% — symulacja SLpoTP1, musi być < mark price)")
    info(f"  size:         0.5 SOL (zmniejszenie z 1.0 po TP1)")
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/modify-tpsl-order", params, dry_run=dry_run)
    raw("modify-tpsl-order", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        ok(f"Modyfikacja zaakceptowana przez API")
        return True
    else:
        fail(f"Modyfikacja odrzucona: code={resp.get('code')} msg={resp.get('msg')}")
        return False


def test_verify_sl_modified(client, sl_oid, expected_price, expected_size):
    sep("[8] Sprawdź czy modyfikacja SL się przyjęła")
    if not sl_oid:
        fail("Brak sl_oid — pomijam weryfikację")
        return

    resp = client.get("/api/v2/mix/order/orders-plan-pending", {
        "symbol":      SYMBOL,
        "productType": PRODUCT_TYPE,
        "planType":    "loss_plan",
    })
    raw("pending loss_plan po modyfikacji", resp)
    if resp.get("code") == "00000":
        for o in resp["data"].get("entrustedList", []):
            if o["orderId"] == sl_oid:
                actual_price = float(o.get("triggerPrice", 0))
                actual_size  = float(o.get("size", 0))
                info(f"SL order znaleziony:")
                info(f"  triggerPrice: {actual_price} (oczekiwano: {expected_price:.2f})")
                info(f"  size:         {actual_size} SOL (oczekiwano: 0.5)")

                price_ok = abs(actual_price - expected_price) < 0.10
                size_ok  = abs(actual_size - 0.5) < 0.05

                if price_ok:
                    ok(f"Cena SL zmieniona poprawnie → ${actual_price:.2f}")
                else:
                    fail(f"Cena SL NIE zmieniona: ${actual_price:.2f} (oczekiwano ~${expected_price:.2f})")

                if size_ok:
                    ok(f"Rozmiar SL zmieniony poprawnie → {actual_size} SOL")
                else:
                    fail(f"Rozmiar SL NIE zmieniony: {actual_size} SOL (oczekiwano 0.5)")
                return
        fail(f"SL order {sl_oid} nie znaleziony w pending po modyfikacji")
    else:
        fail(f"Błąd odpytania: {resp.get('msg')}")


def test_close_position(client, dry_run=False):
    sep("[CLEANUP] Zamknij pozycję long")
    params = {
        "symbol":      SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginMode":  MARGIN_MODE,
        "marginCoin":  MARGIN_COIN,
        "size":        "1",
        "orderType":   "market",
        "side":        "sell",
        "tradeSide":   "close",
        "posSide":     "long",
    }
    info(f"Parametry: {json.dumps(params)}")
    resp = client.post("/api/v2/mix/order/place-order", params, dry_run=dry_run)
    raw("close-position", resp)
    if resp.get("code") in ("00000", "DRY_RUN"):
        ok("Pozycja zamknięta")
    else:
        fail(f"Błąd zamknięcia: code={resp.get('code')} msg={resp.get('msg')}")


# ── Główna funkcja ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--close",   action="store_true", help="Zamknij pozycję po testach")
    parser.add_argument("--dry-run", action="store_true", help="Tylko wypisz parametry, nie wysyłaj")
    args = parser.parse_args()

    key        = os.getenv("BITGET_API_KEY", "")
    secret     = os.getenv("BITGET_API_SECRET", "")
    passphrase = os.getenv("BITGET_PASSPHRASE", "")
    demo       = os.getenv("BITGET_DEMO", "true").lower() != "false"

    if not args.dry_run and (not key or not secret or not passphrase):
        print("❌ Brak BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE")
        sys.exit(1)

    client = BitgetClient(key, secret, passphrase, demo=demo)
    mode   = "DRY-RUN" if args.dry_run else ("DEMO" if demo else "PRODUKCJA")

    print(f"\n{'='*60}")
    print(f"  TEST EXCHANGE — Bitget {SYMBOL} | Tryb: {mode}")
    print(f"{'='*60}")

    results = {}

    # [1] Cena
    if args.dry_run:
        price = 150.0
        sep("[1] Pobierz aktualną cenę SOL")
        info(f"DRY-RUN — używam ceny zastępczej: ${price:.2f}")
    else:
        price = test_get_price(client)
        if price is None:
            print("\n❌ Nie można pobrać ceny — przerywam.")
            sys.exit(1)

    # [2] Otwórz pozycję
    entry_oid = test_open_position(client, price, dry_run=args.dry_run)
    results["open_position"] = entry_oid is not None

    if not args.dry_run:
        info("Czekam 2s na przetworzenie zlecenia...")
        time.sleep(2)

    # [3] TP1
    tp1_oid, tp1_price = test_place_tp1(client, price, dry_run=args.dry_run)
    results["place_tp1"] = tp1_oid is not None

    # [4] TP2
    tp2_oid, tp2_price = test_place_tp2(client, price, dry_run=args.dry_run)
    results["place_tp2"] = tp2_oid is not None

    # [5] SL
    sl_oid, sl_price = test_place_sl(client, price, dry_run=args.dry_run)
    results["place_sl"] = sl_oid is not None

    # [6] Weryfikacja pending
    if not args.dry_run:
        test_verify_tpsl_pending(client, tp1_oid, tp2_oid, sl_oid)

    # [7] Modyfikacja SL
    new_sl_price  = round(price * 1.005, 2)
    modify_ok     = test_modify_sl(client, sl_oid, price, dry_run=args.dry_run)
    results["modify_sl"] = modify_ok

    # [8] Weryfikacja modyfikacji
    if not args.dry_run and modify_ok:
        time.sleep(1)
        test_verify_sl_modified(client, sl_oid, new_sl_price, 0.5)

    # [CLEANUP]
    if args.close:
        test_close_position(client, dry_run=args.dry_run)

    # ── Podsumowanie ───────────────────────────────────────────────────────────
    sep("PODSUMOWANIE")
    all_ok = True
    checks = [
        ("open_position", "Otwieranie pozycji (market order)"),
        ("place_tp1",     "Złożenie TP1 (profit_plan, 0.5 SOL)"),
        ("place_tp2",     "Złożenie TP2 (profit_plan, 0.5 SOL)"),
        ("place_sl",      "Złożenie SL  (loss_plan,   1.0 SOL)"),
        ("modify_sl",     "Modyfikacja SL (cena + size jednocześnie)"),
    ]
    for key_r, label in checks:
        v = results.get(key_r)
        if v:
            print(f"  ✅  {label}")
        elif v is False:
            print(f"  ❌  {label}")
            all_ok = False
        else:
            print(f"  ⚠️   {label} — nie sprawdzono")

    print()
    if all_ok:
        print("  🟢 Wszystkie testy przeszły — mechanizm TPSL działa poprawnie.")
    else:
        print("  🔴 Niektóre testy nie przeszły — sprawdź RAW odpowiedzi powyżej.")

    if not args.close and not args.dry_run:
        print()
        print("  ⚠️  Pozycja pozostaje otwarta na demo.")
        print("      Zamknij ją ręcznie w panelu Bitget lub uruchom ponownie z --close")

    print()


if __name__ == "__main__":
    main()

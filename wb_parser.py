import requests
import time
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# ==============================================================================
# === НАСТРОЙКИ =================================================================
# ==============================================================================
WB_TOKEN = os.getenv("WB_TOKEN")
DAYS_BACK = 30

SELLER_INN = os.getenv("SELLER_INN")
SELLER_FIAS_ID = os.getenv("SELLER_FIAS_ID")

# Параметры для Честного Знака
WITHDRAWAL_TYPE = "DISTANCE"  # DISTANCE (дистанционная продажа) или OTHER
WITHDRAWAL_TYPE_OTHER = "Продажа через маркетплейс Wildberries"  # если тип OTHER
PRIMARY_DOCUMENT_TYPE = "CONSIGNMENT_NOTE"  # тип первичного документа
PRIMARY_DOCUMENT_CUSTOM_NAME = ""  # наименование документа (опционально, можно оставить пустым)

USE_SHORT_KIZ = True  # True = короткий формат (GTIN+SN), False = полный

HISTORY_FILE = "kiz_history.txt"
HISTORY_RETENTION_DAYS = 90
# ==============================================================================

moscow_tz = timezone(timedelta(hours=3))
now_msk = datetime.now(moscow_tz)
orders_start = (now_msk - timedelta(days=DAYS_BACK)).replace(
    hour=0, minute=0, second=0, microsecond=0
)

HEADERS = {
    "Authorization": WB_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

BASE = "https://marketplace-api.wildberries.ru"


def api_request(method, url, max_retries=3, **kwargs):
    delay = 30
    for attempt in range(1, max_retries + 1):
        try:
            if method == "GET":
                r = requests.get(url, headers=HEADERS, timeout=30, **kwargs)
            else:
                r = requests.post(url, headers=HEADERS, timeout=30, **kwargs)
        except Exception as e:
            print(f"   Ошибка сети: {e}. Пауза {delay} сек...")
            time.sleep(delay)
            continue
        if r.status_code == 429:
            print(f"   Лимит 429 (попытка {attempt}/{max_retries}). Пауза {delay} сек...")
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        return r
    return None


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def truncate_kiz(full_kiz):
    """Обрезает КИЗ до формата GTIN+SN"""
    if not full_kiz:
        return full_kiz

    clean_kiz = full_kiz.replace('\x1d', '').replace('\\u001d', '')

    if USE_SHORT_KIZ:
        if len(clean_kiz) >= 31:
            return clean_kiz[:31]

    return clean_kiz


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()
    history = set()
    cutoff_date = now_msk - timedelta(days=HISTORY_RETENTION_DAYS)
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 2:
                    date_str, kiz = parts
                    try:
                        process_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=moscow_tz)
                        if process_date >= cutoff_date:
                            history.add(kiz)
                    except ValueError:
                        history.add(kiz)
    except Exception as e:
        print(f"   Ошибка чтения истории: {e}")
    return history


def save_to_history(kiz_list):
    timestamp = now_msk.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for kiz in kiz_list:
                f.write(f"{timestamp}|{kiz}\n")
    except Exception as e:
        print(f"   Ошибка записи в историю: {e}")


def extract_kiz_from_meta_response(data):
    """Возвращает словарь: {order_id: sgtin}"""
    kiz_map = {}
    orders = data.get("orders", []) if isinstance(data, dict) else []

    for order in orders:
        order_id = order.get("id")
        sgtin = None

        for item in order.get("metaDetails", []) or []:
            if isinstance(item, dict) and item.get("key") == "sgtin":
                sgtin = item.get("value")
                break

        if not sgtin:
            meta = order.get("meta", {}) or {}
            sgtin_obj = meta.get("sgtin")
            if isinstance(sgtin_obj, dict):
                v = sgtin_obj.get("value")
                if isinstance(v, list) and len(v) > 0:
                    sgtin = v[0]
                elif isinstance(v, str):
                    sgtin = v

        if sgtin:
            sgtin = truncate_kiz(sgtin)

        if order_id and sgtin:
            kiz_map[order_id] = sgtin

    return kiz_map


def build_xml_report(supply_id, products, doc_date):
    """
    Формирует XML-документ для Честного Знака.

    Структура:
    <withdrawal version="8">
        <trade_participant_inn>...</trade_participant_inn>
        <withdrawal_type>DISTANCE</withdrawal_type>
        <withdrawal_type_other>...</withdrawal_type_other>
        <withdrawal_date>2026-10-12</withdrawal_date>
        <primary_document_type>CONSIGNMENT_NOTE</primary_document_type>
        <primary_document_number>WB-GI-286461532</primary_document_number>
        <primary_document_date>2026-10-01</primary_document_date>
        <primary_document_custom_name>...</primary_document_custom_name>
        <products_list>
            <product><cis>...</cis></product>
            ...
        </products_list>
    </withdrawal>
    """
    root = ET.Element("withdrawal", version="8")

    ET.SubElement(root, "trade_participant_inn").text = SELLER_INN
    ET.SubElement(root, "withdrawal_type").text = WITHDRAWAL_TYPE

    if WITHDRAWAL_TYPE == "OTHER":
        ET.SubElement(root, "withdrawal_type_other").text = WITHDRAWAL_TYPE_OTHER

    ET.SubElement(root, "withdrawal_date").text = now_msk.strftime("%Y-%m-%d")
    ET.SubElement(root, "primary_document_type").text = PRIMARY_DOCUMENT_TYPE
    ET.SubElement(root, "primary_document_number").text = supply_id
    ET.SubElement(root, "primary_document_date").text = doc_date
    ET.SubElement(root, "fias_id").text = SELLER_FIAS_ID

    if PRIMARY_DOCUMENT_CUSTOM_NAME:
        ET.SubElement(root, "primary_document_custom_name").text = PRIMARY_DOCUMENT_CUSTOM_NAME

    products_list = ET.SubElement(root, "products_list")
    for product in products:
        product_elem = ET.SubElement(products_list, "product")
        ET.SubElement(product_elem, "cis").text = product["cis"]
        ET.SubElement(product_elem, "cost").text = str(product["price"])

    return root


def save_xml(root, filename):
    """Сохраняет XML с правильным форматированием и declaration"""
    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")  # Красивое форматирование (Python 3.9+)

    with open(filename, "wb") as f:
        tree.write(f, encoding="UTF-8", xml_declaration=True)

    print(f"   ✓ XML: {filename} — {len(root.find('products_list'))} КИЗов")


def main():
    if not WB_TOKEN or WB_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("Укажите токен!")
        return

    print("[История] Загружаю историю обработанных КИЗов...")
    history = load_history()
    print(f"   Загружено КИЗов из истории: {len(history)}")
    print(f"   Формат КИЗа: {'короткий (GTIN+SN)' if USE_SHORT_KIZ else 'полный'}")
    print(f"   Тип вывода: {WITHDRAWAL_TYPE}")

    # ---------- Шаг 1: список FBS-заданий ----------
    url = f"{BASE}/api/v3/orders"
    params = {"dateFrom": int(orders_start.timestamp()), "limit": 1000, "next": 0}

    orders_dict = {}
    order_ids = []
    pages = 0
    time.sleep(2)

    print(f"\n[Шаг 1] Список FBS-заданий за {DAYS_BACK} дней...")
    while pages < 100:
        pages += 1
        r = api_request("GET", url, params=params)
        if r is None:
            return
        if r.status_code != 200:
            print(f"Ошибка orders: {r.status_code} - {r.text[:200]}")
            return
        data = r.json()
        orders = data.get("orders", [])
        if not orders:
            break

        for o in orders:
            oid = o.get("id")
            if oid:
                order_ids.append(oid)
                orders_dict[oid] = {
                    "supplyId": o.get("supplyId", f"ORDER_{oid}"),
                    "price": o.get("price", 0),  # ← цена в копейках
                    "createdAt": o.get("createdAt", now_msk.isoformat()),
                }

        nxt = data.get("next")
        if not nxt:
            break
        params["next"] = nxt
        time.sleep(0.5)

    print(f"   Найдено заданий: {len(order_ids)}")
    if not order_ids:
        print("Заданий нет.")
        return

    # ---------- Шаг 2: статусы ----------
    status_url = f"{BASE}/api/v3/orders/status"
    sold_ids = []
    counter = {}

    print("\n[Шаг 2] Запрашиваю статусы...")
    for chunk in chunks(order_ids, 1000):
        time.sleep(0.5)
        r = api_request("POST", status_url, json={"orders": chunk})
        if r is None or r.status_code != 200:
            continue
        for item in r.json().get("orders", []):
            wb = item.get("wbStatus", "?")
            counter[wb] = counter.get(wb, 0) + 1
            if wb == "sold":
                sold_ids.append(item.get("id"))

    print(f"   Раскладка wbStatus: {counter}")
    print(f"   Продано (wbStatus=sold): {len(sold_ids)}")

    if not sold_ids:
        print("\nПроданных FBS-заданий нет.")
        return

    # ---------- Шаг 3: КИЗы ----------
    meta_url = f"{BASE}/api/marketplace/v3/orders/meta"
    kiz_map = {}

    print(f"\n[Шаг 3] Запрашиваю КИЗы для {len(sold_ids)} проданных заданий...")
    for chunk in chunks(sold_ids, 100):
        time.sleep(0.5)
        r = api_request("POST", meta_url, json={"orders": chunk})
        if r is None:
            continue
        if r.status_code != 200:
            print(f"   Ошибка meta: {r.status_code} - {r.text[:200]}")
            continue
        found_map = extract_kiz_from_meta_response(r.json())
        kiz_map.update(found_map)

    # ---------- Шаг 4: Группировка по поставкам ----------
    print("\n[Шаг 4] Группирую КИЗы по поставкам...")

    grouped = defaultdict(list)
    new_kiz_for_history = []
    skipped_already = 0
    skipped_no_kiz = 0

    for order_id in sold_ids:
        sgtin = kiz_map.get(order_id)
        if not sgtin:
            skipped_no_kiz += 1
            continue

        if sgtin in history:
            skipped_already += 1
            continue

        order_info = orders_dict.get(order_id, {})
        supply_id = order_info.get("supplyId", f"ORDER_{order_id}")
        created_at = order_info.get("createdAt", now_msk.isoformat())
        doc_date = created_at.split("T")[0] if "T" in created_at else now_msk.strftime("%Y-%m-%d")

        grouped[supply_id].append({
            "cis": sgtin,
            "price": order_info.get("price", 0),  # ← добавили цену
            "doc_date": doc_date,
        })
        new_kiz_for_history.append(sgtin)

    print(f"   Пропущено (уже в истории): {skipped_already}")
    print(f"   Пропущено (нет КИЗа в WB): {skipped_no_kiz}")
    print(f"   Новых КИЗов: {len(new_kiz_for_history)}")
    print(f"   Поставок с новыми КИЗами: {len(grouped)}")

    if not grouped:
        print("\nНовых КИЗов для вывода нет.")
        return

    # ---------- Шаг 5: Генерация XML для каждой поставки ----------
    print(f"\n[Шаг 5] Формирую XML-файлы для Честного Знака...")

    saved_files = []
    for supply_id, products in grouped.items():
        doc_date = products[0]["doc_date"]  # Дата первого заказа в поставке
        products_clean = [{"cis": p["cis"], "price": p["price"]} for p in products]

        root = build_xml_report(supply_id, products_clean, doc_date)

        safe_supply = supply_id.replace("/", "_").replace("\\", "_")
        filename = f"cz_report_{safe_supply}_{now_msk.strftime('%Y-%m-%d')}.xml"

        save_xml(root, filename)
        saved_files.append((filename, len(products)))

    # Обновляем историю
    save_to_history(new_kiz_for_history)

    print(f"\n{'=' * 60}")
    print(f"✓ УСПЕХ! Сформировано {len(saved_files)} XML-файлов для Честного Знака.")
    print(f"✓ Всего новых КИЗов: {len(new_kiz_for_history)}")


if __name__ == "__main__":
    main()
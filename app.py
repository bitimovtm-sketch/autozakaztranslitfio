import os
import threading
import time
import requests
from flask import Flask, request

app = Flask(__name__)

# Ссылка на входящий вебхук Битрикс24 (для чтения контакта и записи в сделку)
# Задаётся в Railway -> Variables -> BITRIX_WEBHOOK_URL
# Пример: https://autozakaz.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/
BITRIX_WEBHOOK_URL = os.environ.get("BITRIX_WEBHOOK_URL", "").rstrip("/")

# Код поля сделки, куда пишем результат
TARGET_FIELD_CODE = "1673397386370"  # это то, что после UF_CRM_ в UF_CRM_1673397386370

# Таблица транслитерации (стандарт, используемый в загранпаспортах РФ)
TRANSLIT_TABLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "ie", "ы": "y", "ь": "",
    "э": "e", "ю": "iu", "я": "ia",
}


def translit(text):
    """Переводит русский текст в латиницу, буква за буквой."""
    if not text:
        return ""
    result = []
    for char in text:
        lower_char = char.lower()
        if lower_char in TRANSLIT_TABLE:
            translit_char = TRANSLIT_TABLE[lower_char]
            # Сохраняем заглавную букву, если исходная буква была заглавной
            if char.isupper() and translit_char:
                translit_char = translit_char[0].upper() + translit_char[1:]
            result.append(translit_char)
        else:
            result.append(char)
    return "".join(result)


def bitrix_call(method, params):
    """Делает запрос к Битрикс24 через входящий вебхук."""
    url = f"{BITRIX_WEBHOOK_URL}/{method}.json"
    response = requests.post(url, json=params, timeout=15)
    response.raise_for_status()
    return response.json()


# Настройки массовой обработки (можно переопределить в Railway -> Variables)
BATCH_CATEGORY_ID = int(os.environ.get("BATCH_CATEGORY_ID", "10"))
BATCH_MIN_DEAL_ID = int(os.environ.get("BATCH_MIN_DEAL_ID", "812484"))

batch_running = False  # чтобы не запустить обработку дважды одновременно


def process_one_deal(deal_id):
    """Обрабатывает одну сделку: находит контакт, транслитерирует ФИО, записывает в поле."""
    deal_result = bitrix_call("crm.deal.get", {"id": deal_id})
    deal = deal_result.get("result")
    if not deal:
        return False, "сделка не найдена"

    contact_id = deal.get("CONTACT_ID")
    if not contact_id:
        return False, "нет привязанного контакта"

    contact_result = bitrix_call("crm.contact.get", {"id": contact_id})
    contact = contact_result.get("result")
    if not contact:
        return False, "контакт не найден"

    last_name = contact.get("LAST_NAME") or ""
    name = contact.get("NAME") or ""
    second_name = contact.get("SECOND_NAME") or ""

    full_name_ru = " ".join(part for part in [last_name, name, second_name] if part)
    full_name_en = translit(full_name_ru)

    bitrix_call("crm.item.update", {
        "entityTypeId": 2,
        "id": deal_id,
        "fields": {
            f"ufCrm_{TARGET_FIELD_CODE}": full_name_en
        }
    })
    return True, full_name_en


def run_batch_job():
    global batch_running
    batch_running = True
    processed = 0
    skipped = 0
    try:
        start = 0
        while True:
            result = bitrix_call("crm.deal.list", {
                "filter": {
                    "CATEGORY_ID": BATCH_CATEGORY_ID,
                    ">=ID": BATCH_MIN_DEAL_ID,
                    ">CONTACT_ID": 0,
                },
                "select": ["ID", "CONTACT_ID"],
                "order": {"ID": "ASC"},
                "start": start,
            })
            deals = result.get("result", [])
            if not deals:
                break

            for deal in deals:
                deal_id = deal.get("ID")
                try:
                    ok, info = process_one_deal(deal_id)
                    if ok:
                        processed += 1
                        print(f"Сделка {deal_id}: записано '{info}'")
                    else:
                        skipped += 1
                        print(f"Сделка {deal_id}: пропущена ({info})")
                except Exception as e:
                    skipped += 1
                    print(f"Сделка {deal_id}: ошибка {e}")
                time.sleep(0.3)  # пауза, чтобы не превысить лимит запросов Битрикса

            next_start = result.get("next")
            if next_start is None:
                break
            start = next_start

        print(f"=== Массовая обработка завершена. Обработано: {processed}, пропущено: {skipped} ===")
    finally:
        batch_running = False


@app.route("/run-batch", methods=["GET"])
def run_batch():
    global batch_running
    if batch_running:
        return "Обработка уже выполняется, дождитесь завершения", 200
    thread = threading.Thread(target=run_batch_job, daemon=True)
    thread.start()
    return (
        f"Массовая обработка запущена (воронка {BATCH_CATEGORY_ID}, сделки с ID >= {BATCH_MIN_DEAL_ID}). "
        "Прогресс смотрите в Railway -> Deploy Logs.",
        200,
    )


@app.route("/translit-hook", methods=["GET", "POST"])
def translit_hook():
    # Логируем всё, что пришло, чтобы можно было посмотреть в Railway Logs
    print("=== Новый запрос ===")
    print("Метод:", request.method)
    print("Параметры адреса (args):", dict(request.args))
    print("Тело запроса (form):", dict(request.form))
    print("=====================")

    # deal_id может прийти либо в адресе (?deal_id=...) от робота БП,
    # либо в теле запроса (data[FIELDS][ID]) от глобального исходящего вебхука
    deal_id = request.args.get("deal_id")
    if not deal_id:
        data = request.form.to_dict()
        deal_id = data.get("data[FIELDS][ID]")

    if not deal_id:
        return "no deal id", 200

    try:
        ok, info = process_one_deal(deal_id)
    except Exception as e:
        return f"error: {e}", 200

    return ("ok: " + info) if ok else info, 200


@app.route("/", methods=["GET"])
def health():
    return "translit service is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

import os
import threading
import time
import concurrent.futures
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
            result.append(TRANSLIT_TABLE[lower_char])
        else:
            result.append(char)
    return "".join(result)


def to_title_case(text):
    """Приводит к виду 'Имя Фамилия' независимо от регистра исходных данных
    (в Битриксе встречаются контакты, записанные ЗАГЛАВНЫМИ буквами)."""
    words = text.split(" ")
    fixed_words = []
    for word in words:
        parts = word.split("-")
        parts = [(p[:1].upper() + p[1:].lower()) if p else p for p in parts]
        fixed_words.append("-".join(parts))
    return " ".join(fixed_words)


def bitrix_call(method, params, retries=3):
    """Делает запрос к Битрикс24 через входящий вебхук.
    При ошибке превышения лимита запросов (QUERY_LIMIT_EXCEEDED) ждёт и повторяет."""
    url = f"{BITRIX_WEBHOOK_URL}/{method}.json"
    for attempt in range(retries):
        response = requests.post(url, json=params, timeout=15)
        data = response.json()
        error = data.get("error")
        if error == "QUERY_LIMIT_EXCEEDED" and attempt < retries - 1:
            time.sleep(2)
            continue
        response.raise_for_status()
        return data
    return data


# Список ID сделок для массовой обработки — по одному ID на строку
DEAL_IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deal_ids.txt")

batch_running = False  # чтобы не запустить обработку дважды одновременно
stop_requested = False  # флаг для остановки текущего прогона по требованию


def load_deal_ids():
    with open(DEAL_IDS_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]


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
    full_name_en = to_title_case(translit(full_name_ru))

    bitrix_call("crm.item.update", {
        "entityTypeId": 2,
        "id": deal_id,
        "fields": {
            f"ufCrm_{TARGET_FIELD_CODE}": full_name_en
        }
    })
    return True, full_name_en


# Сколько сделок обрабатывать одновременно
BATCH_WORKERS = int(os.environ.get("BATCH_WORKERS", "4"))


def run_batch_job():
    global batch_running
    batch_running = True
    processed = 0
    skipped = 0
    lock = threading.Lock()
    try:
        deal_ids = load_deal_ids()
        total = len(deal_ids)
        print(f"=== Начинаю обработку {total} сделок из deal_ids.txt (по {BATCH_WORKERS} одновременно) ===")

        def handle(deal_id):
            nonlocal processed, skipped
            if stop_requested:
                with lock:
                    skipped += 1
                    done = processed + skipped
                print(f"[{done}/{total}] Сделка {deal_id}: пропущена (остановлено пользователем)")
                return
            try:
                ok, info = process_one_deal(deal_id)
            except Exception as e:
                ok, info = False, f"ошибка {e}"
            with lock:
                if ok:
                    processed += 1
                else:
                    skipped += 1
                done = processed + skipped
            status = "записано" if ok else "пропущена"
            print(f"[{done}/{total}] Сделка {deal_id}: {status} '{info}'")

        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_WORKERS) as executor:
            list(executor.map(handle, deal_ids))

        print(f"=== Массовая обработка завершена. Обработано: {processed}, пропущено: {skipped} ===")
    finally:
        batch_running = False


@app.route("/run-batch", methods=["GET"])
def run_batch():
    global batch_running, stop_requested
    if batch_running:
        return "Обработка уже выполняется, дождитесь завершения", 200
    try:
        total = len(load_deal_ids())
    except FileNotFoundError:
        return "Файл deal_ids.txt не найден рядом с app.py", 200
    stop_requested = False
    thread = threading.Thread(target=run_batch_job, daemon=True)
    thread.start()
    return (
        f"Массовая обработка запущена ({total} сделок из deal_ids.txt, по {BATCH_WORKERS} одновременно). "
        "Прогресс смотрите в Railway -> Deploy Logs.",
        200,
    )


@app.route("/stop-batch", methods=["GET"])
def stop_batch():
    global stop_requested
    if not batch_running:
        return "Сейчас ничего не выполняется", 200
    stop_requested = True
    return "Останавливаю после текущих сделок в работе...", 200


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

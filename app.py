import os
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


@app.route("/translit-hook", methods=["GET", "POST"])
def translit_hook():
    # deal_id может прийти либо в адресе (?deal_id=...) от робота БП,
    # либо в теле запроса (data[FIELDS][ID]) от глобального исходящего вебхука
    deal_id = request.args.get("deal_id")
    if not deal_id:
        data = request.form.to_dict()
        deal_id = data.get("data[FIELDS][ID]")

    if not deal_id:
        return "no deal id", 200

    # Получаем сделку: нужен CONTACT_ID и текущее значение целевого поля
    deal_result = bitrix_call("crm.deal.get", {"id": deal_id})
    deal = deal_result.get("result")
    if not deal:
        return "deal not found", 200

    # Защита от зацикливания: если поле уже заполнено — ничего не делаем
    current_value = deal.get(f"UF_CRM_{TARGET_FIELD_CODE}")
    if current_value:
        return "already filled", 200

    contact_id = deal.get("CONTACT_ID")
    if not contact_id:
        return "no contact linked", 200

    # Получаем ФИО контакта
    contact_result = bitrix_call("crm.contact.get", {"id": contact_id})
    contact = contact_result.get("result")
    if not contact:
        return "contact not found", 200

    last_name = contact.get("LAST_NAME") or ""
    name = contact.get("NAME") or ""
    second_name = contact.get("SECOND_NAME") or ""

    full_name_ru = " ".join(part for part in [last_name, name, second_name] if part)
    full_name_en = translit(full_name_ru)

    # Записываем результат в сделку через crm.item.update (entityTypeId=2 -> Сделка)
    bitrix_call("crm.item.update", {
        "entityTypeId": 2,
        "id": deal_id,
        "fields": {
            f"ufCrm_{TARGET_FIELD_CODE}": full_name_en
        }
    })

    return "ok", 200


@app.route("/", methods=["GET"])
def health():
    return "translit service is running", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

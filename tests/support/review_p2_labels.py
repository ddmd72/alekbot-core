"""The 13 label texts P2 revised adds to a model-written label set (contract C11), in Russian and in Portuguese: the two
languages without UI strings the review tests write sets for. Each test's own 15-key set spreads one of these in, so the
set is exactly `LABEL_KEYS` again (28 keys) and `check_labels` accepts it (the options of each question kind differ)."""
from typing import Dict

from src.domain.ui_messages import UIMessage

RU_P2_LABELS: Dict[str, str] = {
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "Это состоялось?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "Это продлили?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "Это оплачено?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "Состоялось",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "Не состоялось",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Продлено",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "Не продлено",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "Уже не нужно",
    UIMessage.MEMORY_OPTION_PAID.value: "Оплачено",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "Не оплачено",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Записано как история без вашего ответа:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Счета без подтверждения оплаты:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Отметьте оплаченные в кабинете:",
}

PT_P2_LABELS: Dict[str, str] = {
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "Isto aconteceu?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "Isto foi renovado?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "Isto foi pago?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "Aconteceu",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "Não aconteceu",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Renovado",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "Não renovado",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "Já não é preciso",
    UIMessage.MEMORY_OPTION_PAID.value: "Pago",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "Não pago",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Registado como histórico sem a sua resposta:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Faturas sem confirmação de pagamento:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Marque as pagas no Cabinet:",
}

"""
Email card labels — the closed sets of the email card (email index v2, card slice 1) beside the drive's own. Pure
constants.

The card's `doc_type` is the drive's `DOC_TYPES` plus the mail-only values below; `life_domains`, entity types, value
and date labels are the drive's sets unchanged (`drive_labels`). The mail-only types' names live HERE, not in the
drive's `DOC_TYPE_NAMES`: that table is inside the drive keys digest (`index_tables_digest` via `label_names_digest`),
so adding to it would start a re-key pass over every drive. The drive keyer reads type names only from its own table,
so the card builder adds the mail-only names itself, as the profile's label words (`lw:`).

Public interface:
    MAIL_DOC_TYPES                       the mail-only doc types
    CARD_DOC_TYPES                       DOC_TYPES (without `other`) + MAIL_DOC_TYPES + `other`, in that order
    MAIL_DOC_TYPE_NAMES: type -> names   en, es, fr, uk, ru, singular and plural; a multi-word name is keyed per token
    card_type_names(doc_type) -> names   the drive's names of a drive type, the mail table's of a mail-only one
"""
from typing import Mapping, Tuple

from .drive_labels import DOC_TYPE_NAMES, DOC_TYPES

MAIL_DOC_TYPES: Tuple[str, ...] = ("order_update", "appointment", "account_notice", "message", "invitation")

CARD_DOC_TYPES: Tuple[str, ...] = (*(t for t in DOC_TYPES if t != "other"), *MAIL_DOC_TYPES, "other")

MAIL_DOC_TYPE_NAMES: Mapping[str, Tuple[str, ...]] = {
    "order_update": ("order update", "order updates", "order", "orders", "delivery", "deliveries", "shipment",
                     "shipments", "pedido", "pedidos", "envío", "envíos", "commande", "commandes", "livraison",
                     "livraisons", "замовлення", "доставка", "доставки", "заказ", "заказы"),
    "appointment": ("appointment", "appointments", "cita", "citas", "rendez-vous", "прийом", "прийоми", "візит",
                    "візити", "приём", "приёмы", "визит", "визиты"),
    "account_notice": ("account notice", "account notices", "notice", "notices", "aviso", "avisos", "notificación",
                       "notificaciones", "avis", "notification", "notifications", "сповіщення", "уведомление",
                       "уведомления", "извещение", "извещения"),
    "message": ("message", "messages", "mensaje", "mensajes", "повідомлення", "сообщение", "сообщения"),
    "invitation": ("invitation", "invitations", "invite", "invites", "invitación", "invitaciones", "запрошення",
                   "приглашение", "приглашения"),
}


def card_type_names(doc_type: str) -> Tuple[str, ...]:
    """The names of a card's doc type: the drive's table for a drive type, the mail table for a mail-only one, ()
    for `other` and for a value outside both."""
    return MAIL_DOC_TYPE_NAMES.get(doc_type) or tuple(DOC_TYPE_NAMES.get(doc_type, ()))

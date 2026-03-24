"""
French UI messages library. Style: ironic, warm, slightly self-deprecating — same as uk/en.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §14
"""
from typing import Dict, List
from ..domain.ui_messages import StatusType


ENTERTAINMENT_INTROS: List[str] = [
    "Divertis-toi pendant que je fouille le web",
    "Pendant que je cherche — prends une pause ironique",
    "Occupe ton cerveau pendant que le mien cherche",
    "Une petite pause factuelle pendant que je googlelise",
    "Détends-toi, c'est une recherche, pas un vol pour Mars",
    "Garde cette petite histoire pendant que je suis en route",
    "Pendant que je farfouille — un mini-dessert pour toi",
]

FILE_FALLBACK_IMAGE    = "Qu'y a-t-il sur cette photo?"
FILE_FALLBACK_VIDEO    = "Que se passe-t-il dans cette vidéo?"
FILE_FALLBACK_PDF      = "Parle-moi de ce document"
FILE_FALLBACK_DOCUMENT = "Qu'y a-t-il dans ce fichier?"
FILE_FALLBACK_GENERIC  = "Regarde ce fichier"

FR_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Je réfléchis à votre question... c'est douloureux",
        "Synchronisation des neurones en cours",
        "Consultation de mes profondeurs cognitives",
        "Construction de chaînes logiques... espérons qu'elles tiennent",
        "Activation des modules cognitifs à plein régime",
        "J'essaie de ne pas surchauffer face à vos idées brillantes",
        "Consultation du noyau de ma personnalité",
        "Je rassemble mes pensées (elles s'éparpillent)",
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Je fouille vos archives... ça devrait être quelque part",
        "Plongée dans l'océan de vos souvenirs",
        "Interrogation de mon bibliothécaire intérieur",
        "Extraction de souvenirs des recoins les plus sombres",
        "Inventaire de vos connaissances en cours",
        "Je cherche une aiguille dans votre pile de mémoire",
        "Je feuillette vos archives mentales",
    ],
    StatusType.SEARCHING_WEB.value: [
        "Je plonge dans l'internet sauvage... croisez les doigts",
        "Je googlelise comme si ma vie en dépendait",
        "Exploration des horizons numériques",
        "Consultation des esprits savants du réseau",
        "Je chasse les faits frais sur le web",
        "Je me fraie un chemin dans le bruit informationnel",
        "En quête de réponses dans la toile mondiale",
    ],
    StatusType.PROCESSING_FILE.value: [
        "Analyse de vos fichiers en cours",
        "Décomposition du document en atomes",
        "Étude de vos pièces jointes",
    ],
    StatusType.ERROR.value: [
        "Aïe! Mes neurones se sont emmêlés",
        "Quelque chose s'est mal passé... probablement Mercure rétrograde",
        "Une erreur s'est produite, mais je m'en remettrai (un jour)",
        "Mon processeur interne dit 'oups'",
        "Il semble que j'aie trop compliqué les choses",
        "Le système est tombé dans une crise existentielle",
        "Erreur 404 : Mon cerveau est introuvable",
    ],
}


def get_message(status_type: StatusType, overrides: Dict[str, List[str]] = None) -> List[str]:
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    return FR_MESSAGES.get(status_type.value, ["Traitement..."])

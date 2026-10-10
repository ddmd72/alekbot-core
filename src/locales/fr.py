"""
French UI messages library. Style: ironic, warm, slightly self-deprecating — same as uk/en.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §14
"""
from typing import Dict, List, Optional
from ..domain.ui_messages import StatusType, UIMessage


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
        "Je pèse les options... la plupart sont bêtes, je cherche la maligne",
        "Je feuillette mon manuel intérieur 'Comment paraître intelligent'",
        "Je chauffe mes circuits à température de service",
        "Je réfléchis si fort qu'on entend mon ventilateur",
        "Je consulte mon subconscient... il se tait, comme d'habitude",
        "Je coupe le mode 'paresseux', j'active le mode 'génie'",
        "J'invoque la muse... elle est encore en pause clope",
        "Je calcule la réponse dans 47 univers parallèles",
        "Je dépoussière mes algorithmes",
        "Je médite sur votre requête comme un vrai moine",
        "Je redémarre mon bon sens... presque prêt",
        "Je cherche une réponse digne de votre question",
        "Je marque une pause pour l'effet dramatique",
        "Encore une seconde... je suis presque un génie",
        "Je déroule les scénarios où je ne me trompe pas",
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


def get_message(status_type: StatusType, overrides: Optional[Dict[str, List[str]]] = None) -> List[str]:
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    return FR_MESSAGES.get(status_type.value, ["Traitement..."])
# Fixed single-string UI messages (see domain.ui_messages.UIMessage)
UI_STRINGS: Dict[str, str] = {
    UIMessage.RESPONSE_READY.value: "✅ Réponse prête.",
    UIMessage.RESPONSE_TRUNCATED_SUFFIX.value: "\n\n... (réponse tronquée)",
    UIMessage.EMPTY_MODEL_RESPONSE.value: "*(réponse vide du modèle)*",
    UIMessage.UNKNOWN_COMMAND.value: "Commande inconnue : `{command}`",
    UIMessage.NEW_TOPIC_ACK.value: "Nouveau sujet. Historique effacé.",
    UIMessage.SOURCES_HEADING.value: "Sources :",
    UIMessage.CONSOLIDATION_STARTED.value: "🧠 Consolidation de la mémoire lancée…",
    UIMessage.VOICE_REQUEST_FAILED.value: "La demande n'a pas abouti.",
    UIMessage.SKILL_USAGE.value: (
        "Compétences : `$skill list` — vos compétences · `$skill save CODE` — enregistrer un brouillon · "
        "`$skill delete NAME` — supprimer une de vos compétences"
    ),
    UIMessage.SKILL_SAVED.value: "✅ Compétence `{name}` enregistrée (v{version}). Elle est disponible dès votre prochain message.",
    UIMessage.SKILL_DRAFT_NOT_FOUND.value: "❌ Aucun brouillon avec le code `{code}`. Demandez-moi de rédiger la compétence à nouveau.",
    UIMessage.SKILL_NOT_SAVED.value: "❌ Non enregistrée : {reason}",
    UIMessage.SKILL_LIST_HEADER.value: "Vos compétences :",
    UIMessage.SKILL_LIST_EMPTY.value: "Vous n'avez encore aucune compétence personnelle.",
    UIMessage.SKILL_SYSTEM_HEADER.value: "Intégrées :",
    UIMessage.SKILL_DELETED.value: "🗑️ Compétence `{name}` supprimée.",
    UIMessage.SKILL_NOT_FOUND.value: "❌ Aucune compétence nommée `{name}`.",
    UIMessage.SKILL_BUILT_IN.value: "`{name}` est une compétence intégrée et ne peut pas être supprimée.",
    UIMessage.SKILL_UNAVAILABLE.value: "Les compétences ne sont pas disponibles pour le moment.",
    UIMessage.SKILL_PREVIEW_FILE_TITLE.value: "Brouillon de compétence : {name}",
    UIMessage.SKILL_PREVIEW_DELIVERY_FAILED.value: "⚠️ Le brouillon de la compétence n'a pas pu être livré. Demandez-moi de le rédiger à nouveau.",
    UIMessage.SKILL_PREVIEW_REF_FILE_TITLE.value: "Brouillon de compétence : {name} — {path}",
    UIMessage.SKILL_LIST_FILES.value: "fichiers : {count}",
    UIMessage.SKILL_CHANGE_NEW.value: "+ {path} (nouveau, {size})",
    UIMessage.SKILL_CHANGE_NEW_UPLOAD.value: '+ {path} ← "{source}" (votre fichier, {size})',
    UIMessage.SKILL_CHANGE_CHANGED.value: "~ {path} (modifié)",
    UIMessage.SKILL_CHANGE_CHANGED_UPLOAD.value: '~ {path} ← "{source}" (modifié)',
    UIMessage.SKILL_CHANGE_REMOVED.value: "− {path} (supprimé)",
    UIMessage.SKILL_CHANGE_UNCHANGED.value: "= fichiers inchangés : {count}",
    UIMessage.LONG_TURN_NOTICE.value: "⏳ J'y travaille — c'est plus long. Je réponds dès que c'est fini ; vous pouvez continuer à écrire.",
    UIMessage.LATE_ANSWER_PREFIX.value: "[réponse différée]",
    UIMessage.LONG_TURN_FAILED.value: "Je n'ai pas pu terminer — quelque chose a cassé en route.",
    UIMessage.LONG_TURN_CANCELLED.value: "Arrêté, comme demandé.",
    UIMessage.LONG_TURN_LOST.value: "J'ai perdu cette tâche en cours de route (le service a redémarré). Redemandez, s'il vous plaît.",
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Supprimé du disque : {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Dossier supprimé : {path} (fichiers : {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Remplacé : {path} ({before} → {after} ; la version précédente est dans l'historique des versions)",
}

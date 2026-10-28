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
    UIMessage.MEMORY_PREVIEW_FILE_TITLE.value: "Faits pour la mémoire : {count}",
    UIMessage.MEMORY_PREVIEW_HEADER.value: "🧠 Faits pour la mémoire à long terme ({count}) : dans le fichier ci-dessus — rien n'est enregistré tant que vous ne collez pas la commande ci-dessous.",
    UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED.value: "⚠️ La liste des faits n'a pas pu être envoyée. Demandez-moi de les proposer à nouveau.",
    UIMessage.MEMORY_USAGE.value: "Mémoire : `$memory save CODE` — enregistrer les faits que j'ai proposés",
    UIMessage.MEMORY_SAVED.value: "✅ Envoyé en mémoire : {count}. Ils seront trouvables après la consolidation en arrière-plan, en général sous quelques heures.",
    UIMessage.MEMORY_DRAFT_NOT_FOUND.value: "❌ Aucune proposition avec le code `{code}`. Demandez-moi de proposer les faits à nouveau.",
    UIMessage.MEMORY_UNAVAILABLE.value: "L'enregistrement des faits n'est pas disponible pour le moment.",
    UIMessage.DRIVE_NOTES_USAGE.value: "Notes de fichiers : `$drive-notes erase-all` — effacer définitivement toutes tes notes de fichiers et leur historique (je t'envoie un code de confirmation) ; colle `$drive-notes erase CODE` ou `$drive-notes erase-all CODE` pour confirmer",
    UIMessage.DRIVE_NOTES_UNAVAILABLE.value: 'Les notes de fichiers ne sont pas disponibles pour le moment.',
    UIMessage.DRIVE_NOTES_NONE.value: "Tu n'as aucune note de fichier.",
    UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST.value: "❌ Cette demande d'effacement n'existe pas : le code est erroné, expiré ou déjà utilisé. Rien n'a été effacé.",
    UIMessage.LONG_TURN_NOTICE.value: "⏳ J'y travaille — c'est plus long. Je réponds dès que c'est fini ; vous pouvez continuer à écrire.",
    UIMessage.LATE_ANSWER_PREFIX.value: "[réponse différée]",
    UIMessage.LONG_TURN_FAILED.value: "Je n'ai pas pu terminer — quelque chose a cassé en route.",
    UIMessage.LONG_TURN_CANCELLED.value: "Arrêté, comme demandé.",
    UIMessage.LONG_TURN_LOST.value: "J'ai perdu cette tâche en cours de route (le service a redémarré). Redemandez, s'il vous plaît.",
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Supprimé du disque : {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Dossier supprimé : {path} (fichiers : {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Remplacé : {path} ({before} → {after} ; la version précédente est dans l'historique des versions)",
    UIMessage.DRIVE_DELETE_FAILED.value: "⚠️ La suppression de {path} n'a pas abouti.",
    UIMessage.DRIVE_REPLACE_FAILED.value: "⚠️ Le remplacement de {path} n'a pas abouti.",
    UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value: "Ce dossier est géré par le bot et ses fichiers sont réécrits ; pour garder votre propre version, copiez le fichier dans un autre dossier.",
    UIMessage.DRIVE_TRANSCRIPTS_README.value: (
        "# Transcriptions\n\n"
        "Géré par le bot : les fichiers ici peuvent être réécrits ; ne les modifiez pas. "
        "Le bot écrase chaque fichier ici, y compris dans Archive/, donc vos modifications ne restent pas. "
        "Pour garder votre propre version, copiez le fichier dans un autre dossier. "
        "Les transcriptions des enregistrements supprimés sont dans Archive/ ; si vous y supprimez un fichier, "
        "le bot l'oublie."
    ),
    UIMessage.DRIVE_TRANSCRIPT_OPEN_RECORDING.value: "ouvrir l'enregistrement",
    UIMessage.DRIVE_TRANSCRIPT_MANAGED_LINE.value: (
        "Géré par le bot : ce fichier peut être réécrit. Transcription automatique, des erreurs sont possibles."
    ),
    UIMessage.DRIVE_TRANSCRIPT_DURATION_MINUTES.value: "{n} min",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_ONE.value: "{n} intervenant",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_MANY.value: "{n} intervenants",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_SPEECH.value: "Aucune parole n'a été détectée dans cet enregistrement ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LONG.value: "Cet enregistrement est trop long pour être transcrit ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LARGE.value: "Cet enregistrement est trop volumineux pour être transcrit.",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_AUDIO.value: "Cet enregistrement n'a pas de piste audio.",
    UIMessage.DEEP_RESEARCH_DRIVE_FOLDER.value: "Recherches approfondies",
    UIMessage.DEEP_RESEARCH_DRIVE_COPIED.value: "📁 Copié sur ton disque : {path}",
    UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED.value: "Non copié sur ton disque : le disque n'est pas connecté.",
    UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED.value: "Non copié sur ton disque : l'enregistrement a échoué.",
    UIMessage.DRIVE_INACCESSIBLE_NOTICE.value: (
        "⚠️ Fichiers de ton dossier sur le disque que le bot ne peut pas ouvrir à cause de leurs paramètres de "
        "partage : {count}. Pour corriger cela, copie chaque fichier dans un nouveau dossier que tu crées dans le "
        "dossier du bot ; les copies pourront être lues. La liste est jointe."
    ),
    UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE.value: "Fichiers que le bot ne peut pas ouvrir",
    UIMessage.MESSAGE_NOT_PROCESSED_BUSY.value: "⚠️ Je n'ai pas pu traiter ton dernier message car j'étais occupé trop longtemps. Renvoie-le, s'il te plaît.",
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Est-ce toujours vrai ?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Oui",
    UIMessage.MEMORY_OPTION_ENDED.value: "Plus maintenant",
    UIMessage.MEMORY_OPTION_NEVER.value: "Jamais",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Je ne sais plus",
    UIMessage.MEMORY_REPLY_HINT.value: "Réponds à ce message par un chiffre, ou avec tes propres mots.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Merci, c'est noté.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Noté — j'en tiendrai compte.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Déjà répondu, merci.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Ce fait a changé entre-temps : je n'ai pas appliqué la réponse, j'y reviendrai.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Cette question est fermée.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Les faits que j'ai mis de côté cette semaine :",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Pour en rétablir un, dis-moi dans le chat que c'est toujours vrai.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "n'est plus vrai",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "n'a jamais été vrai",
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "Est-ce que ça a eu lieu ?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "L'as-tu renouvelé ?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "Est-ce payé ?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "Ça a eu lieu",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "Pas eu lieu",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Renouvelé",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "Pas renouvelé",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "Plus nécessaire",
    UIMessage.MEMORY_OPTION_PAID.value: "Payé",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "Pas payé",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Enregistré comme historique sans ta réponse :",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Factures sans confirmation de paiement :",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Coche celles qui sont payées dans le Cabinet :",
    UIMessage.DRIVE_NOTE_SAVED.value: "📝 Note enregistrée pour {path} (version {version}) :",
    UIMessage.DRIVE_NOTE_UNCHANGED.value: "📝 La note de {path} dit déjà exactement cela (version {version}) ; rien n'a changé :",
    UIMessage.DRIVE_NOTE_CLEARED.value: "🗑️ Note supprimée de {path}. Son historique est conservé, l'annulation reste possible ; dis « efface l'historique » pour la supprimer définitivement.",
    UIMessage.DRIVE_NOTE_NOTHING_TO_CLEAR.value: "Il n'y avait pas de note pour {path} ; rien n'a changé.",
    UIMessage.DRIVE_NOTE_RESTORED.value: "↩️ Note précédente rétablie pour {path} (version {version}) :",
    UIMessage.DRIVE_NOTE_REDONE.value: "↪️ La modification annulée est rétablie pour {path} (version {version}) :",
    UIMessage.DRIVE_NOTE_RESTORED_EMPTY.value: "↩️ Annulé pour {path} : il n'y avait pas de note avant, elle est donc supprimée (version {version}).",
    UIMessage.DRIVE_NOTE_UNDO_REPEATED.value: "↩️ Déjà annulé pour {path} (version {version}) ; rien n'a changé :",
    UIMessage.DRIVE_NOTE_TYPE.value: "type : {doc_type}",
    UIMessage.DRIVE_NOTE_AREAS.value: "domaines de vie : {areas}",
    UIMessage.DRIVE_NOTE_UNDO_HINT.value: "Pour annuler, dis-le simplement.",
    UIMessage.DRIVE_NOTE_STATUS_SOON.value: "🔎 La recherche l'affichera d'ici quelques minutes.",
    UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF.value: "🔎 La recherche dans le disque est désactivée : la recherche ne l'affichera qu'une fois « Rechercher dans mon disque » activé dans le Cabinet.",
    UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED.value: "🔎 Ce fichier n'est pas dans l'index de recherche (il est dans un dossier que l'index garde en une seule fiche, ou il n'a pas encore été vu) : la recherche n'affiche pas la note.",
    UIMessage.DRIVE_NOTE_STATUS_NO_CARD.value: "🔎 Ce fichier n'a pas de fiche de recherche pour l'instant : la recherche n'affiche pas encore la note.",
    UIMessage.DRIVE_NOTE_STATUS_ARCHIVED.value: "🔎 Le fichier n'est plus sur le disque : seule une recherche dans les fichiers supprimés trouve cette note.",
    UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE.value: "🔎 Son disque n'est pas connecté : la recherche n'affichera pas cette note tant que ce disque n'est pas reconnecté.",
    UIMessage.DRIVE_NOTE_ERASE_REQUEST.value: "🗑️ Pour effacer définitivement la note de {path} et tout son historique (versions : {versions}), colle la commande ci-dessous dans les {minutes} minutes. Rien n'est effacé tant que tu ne l'as pas fait.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST.value: "🗑️ Pour effacer définitivement toutes tes notes de fichiers (notes : {count}) et leur historique, colle la commande ci-dessous dans les {minutes} minutes. Rien n'est effacé tant que tu ne l'as pas fait.",
    UIMessage.DRIVE_NOTE_ERASED.value: "🗑️ Notes effacées définitivement, avec leur historique : {count}.",
    UIMessage.DRIVE_NOTE_ERASE_PARTIAL.value: "🗑️ Notes effacées définitivement : {erased}. Non effacées pour l'instant : {left} ; demande-moi de l'effacer à nouveau.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL.value: "🗑️ Notes effacées définitivement : {erased}. Non effacées pour l'instant : {left} ; renvoie `$drive-notes erase-all` pour les effacer.",
}

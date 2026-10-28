"""
Spanish UI messages library. Style: ironic, warm, slightly self-deprecating — same as uk/en.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §14
"""
from typing import Dict, List, Optional
from ..domain.ui_messages import StatusType, UIMessage


ENTERTAINMENT_INTROS: List[str] = [
    "Entrétenete mientras hurgo en la red",
    "Mientras busco — toma una pausa irónica",
    "Mantén tu cerebro ocupado mientras el mío busca",
    "Una pausa de datos mientras googleo",
    "Relájate, es una búsqueda, no un vuelo a Marte",
    "Guarda esta historia corta mientras estoy en camino",
    "Mientras rebusco — aquí tu mini-postre",
]

FILE_FALLBACK_IMAGE    = "¿Qué hay en esta foto?"
FILE_FALLBACK_VIDEO    = "¿Qué ocurre en este vídeo?"
FILE_FALLBACK_PDF      = "Háblame de este documento"
FILE_FALLBACK_DOCUMENT = "¿Qué hay en este archivo?"
FILE_FALLBACK_GENERIC  = "Echa un vistazo a este archivo"

ES_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Reflexionando sobre tu pregunta... duele un poco",
        "Sincronizando neuronas en curso",
        "Consultando mis profundidades cognitivas",
        "Construyendo cadenas lógicas... esperemos que aguanten",
        "Activando módulos cognitivos a plena potencia",
        "Intentando no sobrecalentarme con tus brillantes ideas",
        "Consultando el núcleo de mi personalidad",
        "Reuniendo mis pensamientos (se escapan solos)",
        "Sopesando opciones... casi todas tontas, busco la lista",
        "Hojeando mi manual interior 'Cómo parecer inteligente'",
        "Calentando mis circuitos a temperatura de servicio",
        "Pensando tan fuerte que se oye mi ventilador",
        "Consultando a mi subconsciente... calla, como siempre",
        "Apagando el modo 'vago', encendiendo el modo 'genio'",
        "Invocando a la musa... está otra vez en su descanso",
        "Calculando la respuesta en 47 universos paralelos",
        "Quitándole el polvo a mis algoritmos",
        "Meditando sobre tu petición como un buen monje",
        "Reiniciando mi sentido común... casi listo",
        "Buscando una respuesta digna de tu pregunta",
        "Haciendo una pausa para el efecto dramático",
        "Un segundo más... casi soy un genio",
        "Repasando los escenarios donde no me equivoco",
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Revisando tus archivos... debería estar por aquí",
        "Zambulléndome en el océano de tus recuerdos",
        "Consultando a mi bibliotecario interior",
        "Extrayendo recuerdos de los rincones más oscuros",
        "Inventario de tus conocimientos en curso",
        "Buscando una aguja en tu pila de memoria",
        "Ojeando tus archivos mentales",
    ],
    StatusType.SEARCHING_WEB.value: [
        "Me adentro en el internet salvaje... cruzad los dedos",
        "Googleando como si mi vida dependiera de ello",
        "Explorando los horizontes digitales",
        "Consultando a los sabios de la red",
        "Cazando datos frescos en la web",
        "Abriéndome paso entre el ruido informacional",
        "En busca de respuestas en la red mundial",
    ],
    StatusType.PROCESSING_FILE.value: [
        "Analizando tus archivos",
        "Descomponiendo el documento en átomos",
        "Estudiando tus adjuntos",
    ],
    StatusType.ERROR.value: [
        "¡Ay! Mis neuronas se enredaron",
        "Algo salió mal... probablemente Mercurio retrógrado",
        "Ocurrió un error, pero me recuperaré (algún día)",
        "Mi procesador interno dice 'ups'",
        "Parece que me compliqué demasiado",
        "El sistema entró en crisis existencial",
        "Error 404: Mi cerebro no encontrado",
    ],
}


def get_message(status_type: StatusType, overrides: Optional[Dict[str, List[str]]] = None) -> List[str]:
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    return ES_MESSAGES.get(status_type.value, ["Procesando..."])
# Fixed single-string UI messages (see domain.ui_messages.UIMessage)
UI_STRINGS: Dict[str, str] = {
    UIMessage.RESPONSE_READY.value: "✅ Respuesta lista.",
    UIMessage.RESPONSE_TRUNCATED_SUFFIX.value: "\n\n... (respuesta truncada)",
    UIMessage.EMPTY_MODEL_RESPONSE.value: "*(respuesta vacía del modelo)*",
    UIMessage.UNKNOWN_COMMAND.value: "Comando desconocido: `{command}`",
    UIMessage.NEW_TOPIC_ACK.value: "Nuevo tema. Historial borrado.",
    UIMessage.SOURCES_HEADING.value: "Fuentes:",
    UIMessage.CONSOLIDATION_STARTED.value: "🧠 Consolidación de memoria iniciada…",
    UIMessage.VOICE_REQUEST_FAILED.value: "La solicitud no se pudo completar.",
    UIMessage.SKILL_USAGE.value: (
        "Habilidades: `$skill list` — tus habilidades · `$skill save CODE` — guardar un borrador · "
        "`$skill delete NAME` — eliminar una de tus habilidades"
    ),
    UIMessage.SKILL_SAVED.value: "✅ Habilidad `{name}` guardada (v{version}). Está disponible desde tu próximo mensaje.",
    UIMessage.SKILL_DRAFT_NOT_FOUND.value: "❌ No hay ningún borrador con el código `{code}`. Pídeme que redacte la habilidad otra vez.",
    UIMessage.SKILL_NOT_SAVED.value: "❌ No guardada: {reason}",
    UIMessage.SKILL_LIST_HEADER.value: "Tus habilidades:",
    UIMessage.SKILL_LIST_EMPTY.value: "Todavía no tienes habilidades propias.",
    UIMessage.SKILL_SYSTEM_HEADER.value: "Integradas:",
    UIMessage.SKILL_DELETED.value: "🗑️ Habilidad `{name}` eliminada.",
    UIMessage.SKILL_NOT_FOUND.value: "❌ No hay ninguna habilidad llamada `{name}`.",
    UIMessage.SKILL_BUILT_IN.value: "`{name}` es una habilidad integrada y no se puede eliminar.",
    UIMessage.SKILL_UNAVAILABLE.value: "Las habilidades no están disponibles en este momento.",
    UIMessage.SKILL_PREVIEW_FILE_TITLE.value: "Borrador de habilidad: {name}",
    UIMessage.SKILL_PREVIEW_DELIVERY_FAILED.value: "⚠️ No se pudo entregar el borrador de la habilidad. Pídeme que lo redacte otra vez.",
    UIMessage.SKILL_PREVIEW_REF_FILE_TITLE.value: "Borrador de habilidad: {name} — {path}",
    UIMessage.SKILL_LIST_FILES.value: "archivos: {count}",
    UIMessage.SKILL_CHANGE_NEW.value: "+ {path} (nuevo, {size})",
    UIMessage.SKILL_CHANGE_NEW_UPLOAD.value: '+ {path} ← "{source}" (tu archivo, {size})',
    UIMessage.SKILL_CHANGE_CHANGED.value: "~ {path} (modificado)",
    UIMessage.SKILL_CHANGE_CHANGED_UPLOAD.value: '~ {path} ← "{source}" (modificado)',
    UIMessage.SKILL_CHANGE_REMOVED.value: "− {path} (eliminado)",
    UIMessage.SKILL_CHANGE_UNCHANGED.value: "= archivos sin cambios: {count}",
    UIMessage.MEMORY_PREVIEW_FILE_TITLE.value: "Hechos para la memoria: {count}",
    UIMessage.MEMORY_PREVIEW_HEADER.value: "🧠 Hechos para la memoria a largo plazo ({count}): en el archivo de arriba — no se guarda nada hasta que pegues el comando de abajo.",
    UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED.value: "⚠️ No se pudo enviar la lista de hechos. Pídeme que los proponga de nuevo.",
    UIMessage.MEMORY_USAGE.value: "Memoria: `$memory save CÓDIGO` — guardar los hechos que propuse",
    UIMessage.MEMORY_SAVED.value: "✅ Enviado a la memoria: {count}. Se podrán buscar tras la consolidación en segundo plano, normalmente en unas horas.",
    UIMessage.MEMORY_DRAFT_NOT_FOUND.value: "❌ No hay ninguna propuesta con el código `{code}`. Pídeme que proponga los hechos de nuevo.",
    UIMessage.MEMORY_UNAVAILABLE.value: "Guardar hechos no está disponible en este momento.",
    UIMessage.DRIVE_NOTES_USAGE.value: 'Notas de archivos: `$drive-notes erase-all` — borrar para siempre todas tus notas de archivos y su historial (te envío un código para confirmar); pega `$drive-notes erase CÓDIGO` o `$drive-notes erase-all CÓDIGO` para confirmar',
    UIMessage.DRIVE_NOTES_UNAVAILABLE.value: 'Las notas de archivos no están disponibles en este momento.',
    UIMessage.DRIVE_NOTES_NONE.value: 'No tienes notas de archivos.',
    UIMessage.DRIVE_NOTES_NO_SUCH_REQUEST.value: '❌ No existe esa solicitud de borrado: el código es incorrecto, caducó o ya se usó. No se borró nada.',
    UIMessage.LONG_TURN_NOTICE.value: "⏳ Estoy en ello — tardará más. Responderé al terminar; puedes seguir escribiendo.",
    UIMessage.LATE_ANSWER_PREFIX.value: "[respuesta diferida]",
    UIMessage.LONG_TURN_FAILED.value: "No pude terminarlo: algo falló por el camino.",
    UIMessage.LONG_TURN_CANCELLED.value: "Detenido, como pediste.",
    UIMessage.LONG_TURN_LOST.value: "Perdí esta tarea a mitad de camino (el servicio se reinició). Pregunta de nuevo, por favor.",
    UIMessage.DRIVE_DELETED_FILE.value: "🗑️ Eliminado del disco: {path}",
    UIMessage.DRIVE_DELETED_FOLDER.value: "🗑️ Carpeta eliminada: {path} (archivos: {count})",
    UIMessage.DRIVE_REPLACED.value: "✏️ Reemplazado: {path} ({before} → {after}; la versión anterior está en su historial de versiones)",
    UIMessage.DRIVE_DELETE_FAILED.value: "⚠️ La eliminación de {path} no se completó.",
    UIMessage.DRIVE_REPLACE_FAILED.value: "⚠️ El reemplazo de {path} no se completó.",
    UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value: "Esta carpeta la gestiona el bot y sus archivos se sobrescriben; para conservar tu propia versión, copia el archivo a otra carpeta.",
    UIMessage.DRIVE_TRANSCRIPTS_README.value: (
        "# Transcripciones\n\n"
        "Gestionado por el bot: los archivos de aquí pueden reescribirse; no los edites. "
        "El bot sobrescribe cada archivo de aquí, también en Archive/, así que tus cambios no se conservan. "
        "Para conservar tu propia versión, copia el archivo a otra carpeta. "
        "Las transcripciones de las grabaciones eliminadas están en Archive/; si borras un archivo de allí, "
        "el bot lo olvida."
    ),
    UIMessage.DRIVE_TRANSCRIPT_OPEN_RECORDING.value: "abrir la grabación",
    UIMessage.DRIVE_TRANSCRIPT_MANAGED_LINE.value: (
        "Gestionado por el bot: este archivo puede reescribirse. Transcripción automática, puede contener errores."
    ),
    UIMessage.DRIVE_TRANSCRIPT_DURATION_MINUTES.value: "{n} min",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_ONE.value: "{n} hablante",
    UIMessage.DRIVE_TRANSCRIPT_SPEAKERS_MANY.value: "{n} hablantes",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_SPEECH.value: "No se detectó voz en esta grabación ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LONG.value: "Esta grabación es demasiado larga para transcribirla ({duration}).",
    UIMessage.DRIVE_TRANSCRIPT_STUB_TOO_LARGE.value: "Esta grabación es demasiado grande para transcribirla.",
    UIMessage.DRIVE_TRANSCRIPT_STUB_NO_AUDIO.value: "Esta grabación no tiene pista de audio.",
    UIMessage.DEEP_RESEARCH_DRIVE_FOLDER.value: "Investigaciones",
    UIMessage.DEEP_RESEARCH_DRIVE_COPIED.value: "📁 Copiado en tu disco: {path}",
    UIMessage.DEEP_RESEARCH_DRIVE_NOT_CONNECTED.value: "No copiado en tu disco: el disco no está conectado.",
    UIMessage.DEEP_RESEARCH_DRIVE_COPY_FAILED.value: "No copiado en tu disco: no se pudo guardar.",
    UIMessage.DRIVE_INACCESSIBLE_NOTICE.value: (
        "⚠️ Archivos de tu carpeta en el disco que el bot no puede abrir por su configuración de uso compartido: "
        "{count}. Para solucionarlo, copia cada archivo en una carpeta nueva que crees dentro de la carpeta del bot; "
        "las copias se podrán leer. La lista va adjunta."
    ),
    UIMessage.DRIVE_INACCESSIBLE_LIST_TITLE.value: "Archivos que el bot no puede abrir",
    UIMessage.MESSAGE_NOT_PROCESSED_BUSY.value: "⚠️ No pude procesar tu último mensaje porque estuve ocupado demasiado tiempo. Envíalo de nuevo, por favor.",
    UIMessage.MEMORY_REVIEW_QUESTION.value: "¿Sigue siendo cierto?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Sí",
    UIMessage.MEMORY_OPTION_ENDED.value: "Ya no",
    UIMessage.MEMORY_OPTION_NEVER.value: "Nunca",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "No me acuerdo",
    UIMessage.MEMORY_REPLY_HINT.value: "Responde a este mensaje con un número o con tus propias palabras.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Gracias, anotado.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Anotado: lo tendré en cuenta.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Ya respondiste, gracias.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Ese dato ha cambiado entretanto, así que no apliqué tu respuesta; volveré a él.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Esta pregunta está cerrada.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Datos que dejé de lado esta semana:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Para recuperar uno, dime en el chat que sigue siendo cierto.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "ya no es cierto",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "nunca fue cierto",
    UIMessage.MEMORY_REVIEW_QUESTION_HAPPENED.value: "¿Esto ocurrió?",
    UIMessage.MEMORY_REVIEW_QUESTION_RENEWED.value: "¿Lo renovaste?",
    UIMessage.MEMORY_REVIEW_QUESTION_PAID.value: "¿Está pagado?",
    UIMessage.MEMORY_OPTION_HAPPENED.value: "Sí, ocurrió",
    UIMessage.MEMORY_OPTION_NOT_HAPPENED.value: "No ocurrió",
    UIMessage.MEMORY_OPTION_RENEWED.value: "Renovado",
    UIMessage.MEMORY_OPTION_NOT_RENEWED.value: "No renovado",
    UIMessage.MEMORY_OPTION_NOT_NEEDED.value: "Ya no hace falta",
    UIMessage.MEMORY_OPTION_PAID.value: "Pagado",
    UIMessage.MEMORY_OPTION_NOT_PAID.value: "No pagado",
    UIMessage.MEMORY_DIGEST_HISTORY_HEADER.value: "Registrado como historia sin tu respuesta:",
    UIMessage.MEMORY_DIGEST_INVOICES_HEADER.value: "Facturas sin confirmación de pago:",
    UIMessage.MEMORY_DIGEST_INVOICES_CABINET.value: "Marca las pagadas en el Cabinet:",
    UIMessage.DRIVE_NOTE_SAVED.value: "📝 Nota guardada para {path} (versión {version}):",
    UIMessage.DRIVE_NOTE_UNCHANGED.value: "📝 La nota de {path} ya dice exactamente esto (versión {version}); no ha cambiado nada:",
    UIMessage.DRIVE_NOTE_CLEARED.value: "🗑️ Nota eliminada de {path}. Su historial se conserva, así que se puede deshacer; di «borra el historial» para borrarla para siempre.",
    UIMessage.DRIVE_NOTE_NOTHING_TO_CLEAR.value: "No había ninguna nota para {path}; no ha cambiado nada.",
    UIMessage.DRIVE_NOTE_RESTORED.value: "↩️ Nota anterior restaurada para {path} (versión {version}):",
    UIMessage.DRIVE_NOTE_REDONE.value: "↪️ El cambio deshecho vuelve a estar en {path} (versión {version}):",
    UIMessage.DRIVE_NOTE_RESTORED_EMPTY.value: "↩️ Deshecho en {path}: antes no había nota, así que se ha eliminado (versión {version}).",
    UIMessage.DRIVE_NOTE_UNDO_REPEATED.value: "↩️ Ya estaba deshecho en {path} (versión {version}); no ha cambiado nada:",
    UIMessage.DRIVE_NOTE_TYPE.value: "tipo: {doc_type}",
    UIMessage.DRIVE_NOTE_AREAS.value: "áreas de vida: {areas}",
    UIMessage.DRIVE_NOTE_UNDO_HINT.value: "Para deshacerlo, solo dilo.",
    UIMessage.DRIVE_NOTE_STATUS_SOON.value: "🔎 La búsqueda la mostrará en unos minutos.",
    UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF.value: "🔎 La búsqueda en el disco está desactivada: la búsqueda la mostrará solo cuando actives «Buscar en mi disco» en el Cabinet.",
    UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED.value: "🔎 Este archivo no está en el índice de búsqueda (está dentro de una carpeta que el índice guarda como una sola ficha, o aún no se ha visto), así que la búsqueda no muestra la nota.",
    UIMessage.DRIVE_NOTE_STATUS_NO_CARD.value: "🔎 Este archivo no tiene ficha de búsqueda por ahora, así que la búsqueda aún no muestra la nota.",
    UIMessage.DRIVE_NOTE_STATUS_ARCHIVED.value: "🔎 El archivo ya no está en el disco: solo una búsqueda en los archivos eliminados encuentra esta nota.",
    UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE.value: "🔎 Su disco no está conectado, así que la búsqueda no mostrará esta nota hasta que ese disco se vuelva a conectar.",
    UIMessage.DRIVE_NOTE_ERASE_REQUEST.value: "🗑️ Para borrar para siempre la nota de {path} y todo su historial (versiones: {versions}), pega el comando de abajo en los próximos {minutes} minutos. No se borra nada hasta que lo hagas.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST.value: "🗑️ Para borrar para siempre todas tus notas de archivos (notas: {count}) y su historial, pega el comando de abajo en los próximos {minutes} minutos. No se borra nada hasta que lo hagas.",
    UIMessage.DRIVE_NOTE_ERASED.value: "🗑️ Notas borradas para siempre, con su historial: {count}.",
    UIMessage.DRIVE_NOTE_ERASE_PARTIAL.value: "🗑️ Notas borradas para siempre: {erased}. No borradas por ahora: {left}; pídeme que la borre de nuevo.",
    UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL.value: "🗑️ Notas borradas para siempre: {erased}. No borradas por ahora: {left}; envía `$drive-notes erase-all` otra vez para borrarlas.",
}

"""
Spanish UI messages library. Style: ironic, warm, slightly self-deprecating — same as uk/en.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §14
"""
from typing import Dict, List
from ..domain.ui_messages import StatusType


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


def get_message(status_type: StatusType, overrides: Dict[str, List[str]] = None) -> List[str]:
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    return ES_MESSAGES.get(status_type.value, ["Procesando..."])

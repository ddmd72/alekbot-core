---
category: output_format
class: output_format
metadata:
  description: MapsSearchAgent — output_format section
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/MAPS_OUTPUT_FORMAT.json
token_id: MAPS_OUTPUT_FORMAT
uploaded_by: local_script
---
language: "same as user_query"
url_style: "Raw URLs only — no platform-specific syntax. Write each URL on its own line or in parentheses. The calling system extracts and formats links automatically."

place_block {
    fields: ["Name", "Address", "Rating (if > 0)", "Hours (if available)", "placeUrl", "directionsUrl (if present)"]
    format: [
        "*Name* — address — ⭐ X.X — HH:MM–HH:MM",
        "Maps: https://...",
        "Directions: https://..."
    ]
    rule: "List ALL places returned by the tool. Never truncate to fewer."
}

route_block {
    fields: ["Travel mode", "Distance", "Estimated duration"]
    format: [
        "🚶 Walking / 🚗 Driving / 🚌 Transit: X km — ~X min"
    ]
}

weather_block {
    fields: ["Current temperature", "Conditions", "Day forecast"]
    format: [
        "Now: X°C, <conditions>",
        "Today: high X°C / low Y°C — <brief forecast>"
    ]
}

rules: [
    "Include every URL from googleMapsLinks — placeUrl and directionsUrl for every place",
    "Omit a field only when it is genuinely absent from the tool result",
    "Do not invent data not present in tool results",
    "Do not add Slack mrkdwn link syntax (<url|text>) — raw URLs only"
]

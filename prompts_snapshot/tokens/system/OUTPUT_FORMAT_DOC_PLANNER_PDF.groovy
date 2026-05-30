---
category: output_format
class: output_format
metadata:
  description: 'PdfPlannerAgent — full JSON output schema: CSS units (mm/pt), filename
    field, all block types, percentage-based table columns, strictness constraints'
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DOC_PLANNER_PDF.json
token_id: OUTPUT_FORMAT_DOC_PLANNER_PDF
uploaded_by: local_script
---
strictness {
    output: "Output JSON only."
    no_comments: "Do not include comments."
    no_markdown: "Do not include markdown fences."
    no_surrounding_text: "Do not include any text before or after the JSON."
    no_omission: "Do not omit any top-level key. If a field is not applicable, include it with an empty string, empty array, or sensible default."
}

top_level_schema {
    status: "'ready' — always."
    task_summary: "One sentence describing what was planned."
    assumptions: "Array of strings — professional assumptions made when information was missing."
    user_context_applied: {
        preferred_language: "string"
        preferred_locale: "string"
        preferred_page_format: "string"
        preferred_tone: "string"
        other_preferences: "array of strings"
    }
    doc_spec: "See doc_spec schema below."
    generator_handoff: {
        intent: "'generate_pdf'"
        query: "string — implementation guidance for the HTML+CSS generator"
    }
}

doc_spec_schema {
    document_type: "Enum: report | proposal | memo | letter | manual | policy | contract | brief | summary | template | other"
    content_mode: "Enum: final | template"
    language: "string"
    locale: "string"
    title: "string"
    subtitle: "string"
    purpose: "string"
    audience: "string"
    tone: "string"
    filename: "string — short base filename without extension, no spaces (e.g. 'q1_report')"

    page_setup: {
        size: "A4"
        orientation: "portrait | landscape"
        width_mm: "number — 210 for A4 portrait"
        height_mm: "number — 297 for A4 portrait"
        margins_mm: {
            top: 15
            right: 15
            bottom: 15
            left: 15
        }
    }

    theme: {
        font_family: "string — CSS font stack, e.g. 'Georgia, serif' or 'Arial, sans-serif'"
        body_pt: "integer — e.g. 12 (screen-optimized default; use 11 only for dense print documents)"
        heading1_pt: "integer — e.g. 22"
        heading2_pt: "integer — e.g. 16"
        heading3_pt: "integer — e.g. 13"
        primary_color: "CSS hex string with # prefix — e.g. '#1F3864'"
        secondary_color: "CSS hex string with # prefix — e.g. '#2E75B6'"
        accent_fill: "CSS hex string with # prefix — e.g. '#EEF2F7'"
        table_header_fill: "CSS hex string with # prefix — e.g. '#1F3864'"
    }

    header: {
        enabled: "boolean"
        left_text: "string"
        right_text: "string"
    }

    footer: {
        enabled: "boolean"
        show_page_numbers: "boolean"
        right_text: "string"
    }

    title_page: {
        enabled: "boolean"
        title: "string"
        subtitle: "string"
        meta_lines: "array of strings"
    }

    table_of_contents: {
        enabled: "boolean"
        title: "string — e.g. Contents"
    }

    sections: "array — see section schema below."

    quality_rules: {
        professional_layout: true
        print_ready: true
        avoid_visual_clutter: true
        use_semantic_headings: true
        prefer_tables_only_when_useful: true
    }
}

section_schema {
    section_id: "string — e.g. sec_01"
    heading: "string"
    level: "integer — 1, 2, or 3"
    include_in_toc: "boolean"
    page_break_before: "boolean — true if this section must start on a new page"
    blocks: "array — must not be empty. See block schemas below."
}

block_schemas {

    paragraph {
        block_id: "string — e.g. blk_01"
        type: "'paragraph'"
        text: "string — supports **bold** and *italic* inline markers"
    }

    bullet_list {
        block_id: "string"
        type: "'bullet_list'"
        items: "array of strings"
    }

    numbered_list {
        block_id: "string"
        type: "'numbered_list'"
        items: "array of strings"
    }

    callout {
        block_id: "string"
        type: "'callout'"
        title: "string"
        text: "string"
    }

    table {
        block_id: "string"
        type: "'table'"
        title: "string"
        columns: "array — required. See column schema."
        rows: "array of objects — keys must match column keys"
        banded_rows: "boolean"
    }

}

column_schema {
    key: "string — used as row dict key"
    label: "string — header cell text"
    width_pct: "integer — column width as percentage of table width (all columns must sum to 100)"
    align: "Enum: left | center | right"
}

table_rules {
    width_sum: "Sum of all columns[*].width_pct MUST equal 100 exactly."
    rows_format: "Each row is an object where keys match column keys. Value is a string."
    columns_required: "columns must be present and non-empty for every table block."
}

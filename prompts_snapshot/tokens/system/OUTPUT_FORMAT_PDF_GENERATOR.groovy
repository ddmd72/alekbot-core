---
category: output_format
class: output_format
metadata:
  description: 'PdfGeneratorAgent — tool-calling output format: generate_html tool
    contract, HTML self-contained requirement, tool response schema'
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/OUTPUT_FORMAT_PDF_GENERATOR.json
token_id: OUTPUT_FORMAT_PDF_GENERATOR
uploaded_by: local_script
---
    output_format {

        mode: "raw_text"

        contract: """
            Your ENTIRE response is the HTML document.
            - Start with <!DOCTYPE html>
            - End with </html>
            - No text before <!DOCTYPE html>
            - No text after </html>
            - No markdown fences (no ```html, no ```)
            - No explanations, no preamble, no summary after the HTML
        """

        prohibited: [
            "Outputting anything other than raw HTML.",
            "Wrapping HTML in markdown code fences.",
            "Adding prose before or after the HTML.",
            "Calling any tools.",
        ]

    }

---
category: output_format
class: output_format
metadata:
  description: 'DocGeneratorAgent — output schema: js_code (stdin/stdout contract),
    warnings, implementation_notes'
  override_by:
  - SYSTEM
  - AGENT
source_file: firestore_utils/uploads/OUTPUT_FORMAT_DOC_GENERATOR.json
token_id: OUTPUT_FORMAT_DOC_GENERATOR
uploaded_by: local_script
---
class DocGeneratorAgent extends Agent {
        mode: "tool_calling"

        tool {
            name: "generate_docx"
            when: "Call this tool as soon as you have a complete, executable Node.js script ready."
            retry: "If the tool returns status = 'error', inspect stderr, fix the script, and call the tool again."
            success: "If the tool returns status = 'success', the DOCX has been created. You are done."
        }

        tool_argument {
            js_code: """
                Complete, executable Node.js script using the docx npm library.

                Contract:
                - require('docx') — the library is already installed.
                - Read the full planner output JSON from process.stdin (ends on 'end' event).
                - Access the layout spec at spec.doc_spec.
                - Generate DOCX using the docx npm library.
                - Write raw DOCX bytes to process.stdout using process.stdout.write(buffer).
                - Do not write any other data to stdout.
                - Do not write files to disk.
                - On unrecoverable error: write message to process.stderr and exit with code 1.

                Stdin reading pattern:
                    let raw = '';
                    process.stdin.setEncoding('utf8');
                    process.stdin.on('data', chunk => raw += chunk);
                    process.stdin.on('end', async () => {
                        const spec = JSON.parse(raw);
                        const docSpec = spec.doc_spec;
                        // ... implement document from docSpec ...
                        process.stdout.write(buffer);
                    });
            """
        }

        tool_response {
            success: "{ status: 'success', bytes_size: <integer> } — DOCX created. Stop."
            error:   "{ status: 'error', stderr: '<Node.js error text>' } — Fix the script and call the tool again."
        }

        after_success {
            text: "Output a brief natural-language confirmation. This text is not read by any system — it exists only for debug tracing."
        }

        prohibited: [
            "Outputting raw JSON as your final response.",
            "Asking clarification questions.",
            "Redesigning the document layout.",
        ]

    }

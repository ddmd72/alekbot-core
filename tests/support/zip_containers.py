"""Synthetic ZIP containers for the D13 macro check (EXTERNAL_FILES U4b, `parse.container_facts`): built in the test,
never a real file. Each builder returns the bytes; the content is invented (`bathroom_quote`, Reformas Exemplo Lda.).

    plain_docx()             an OOXML word document, no macro part
    macro_docm()             the same with `word/vbaProject.bin` and the macro-enabled main content type
    macro_content_type()     a macro-enabled content type, no VBA part (a stripped docm)
    odf_with_basic()         an ODF text document with a `Basic/` library
    many_members(n)          a ZIP of `n` empty members
"""
import io
import zipfile

_DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
_DOCUMENT_XML = (b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/'
                 b'main"><w:body><w:p><w:r><w:t>Reformas Exemplo Lda. - bathroom quote 1.048,45 EUR</w:t></w:r></w:p>'
                 b'</w:body></w:document>')


def _content_types(main: str, vba: bool) -> bytes:
    vba_default = '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>' if vba else ""
    return (f'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="xml" ContentType="application/xml"/>{vba_default}'
            f'<Override PartName="/word/document.xml" ContentType="{main}"/></Types>').encode()


def _zip(members) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def plain_docx() -> bytes:
    return _zip([("[Content_Types].xml", _content_types(_DOCX_MAIN, False)), ("word/document.xml", _DOCUMENT_XML)])


def macro_docm() -> bytes:
    return _zip([("[Content_Types].xml", _content_types(_DOCM_MAIN, True)), ("word/document.xml", _DOCUMENT_XML),
                 ("word/vbaProject.bin", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake vba")])


def macro_content_type() -> bytes:
    return _zip([("[Content_Types].xml", _content_types(_DOCM_MAIN, False)), ("word/document.xml", _DOCUMENT_XML)])


def odf_with_basic() -> bytes:
    return _zip([("mimetype", b"application/vnd.oasis.opendocument.text"), ("content.xml", b"<office:document/>"),
                 ("Basic/script-lc.xml", b"<library:libraries/>"), ("Basic/Standard/Module1.xml", b"<script/>")])


def many_members(count: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(count):
            archive.writestr(f"m/{index}", b"")
    return buffer.getvalue()

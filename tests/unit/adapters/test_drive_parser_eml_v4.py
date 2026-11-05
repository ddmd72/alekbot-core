"""`.eml` v4 (EXTERNAL_FILES_RFC §7 U5 (4), A-U5-3/4, owner O-5): every header, every text part, nested messages,
attachments as names, types and sizes only."""
import email.message
import email.policy

from src.adapters.drive_parser import readers
from src.adapters.drive_parser.readers import read_content


def _message(**headers):
    message = email.message.EmailMessage(policy=email.policy.default)
    message["From"] = "Lúcia Exemplo <landlord@example.org>"
    message["To"] = "Marco Teste <tenant@example.org>"
    message["Subject"] = "Renda de novembro"
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    return message


def _read(tmp_path, message):
    path = tmp_path / "m.eml"
    path.write_bytes(message.as_bytes())
    return read_content(str(path), "eml")


def test_every_header_is_read_and_an_rfc2047_subject_is_decoded(tmp_path):
    message = _message(Sender="office@example.org", Reply_To="reply@example.org", Cc="Clara Moreira <clara@example.com>",
                       Bcc="hidden@example.org", Date="Mon, 02 Nov 2026 10:00:00 +0100")
    del message["Subject"]
    message["Subject"] = "=?utf-8?b?0J7RgNC10L3QtNCwINC30LAg0LvQuNGB0YLQvtC/0LDQtA==?="
    message.set_content("Rent for the Lisbon flat is 950 € per month.")
    text = _read(tmp_path, message).text
    for line in ("From: Lúcia Exemplo <landlord@example.org>", "Sender: office@example.org",
                 "Reply-To: reply@example.org", "To: Marco Teste <tenant@example.org>",
                 "Cc: Clara Moreira <clara@example.com>", "Bcc: hidden@example.org",
                 "Date: Mon, 02 Nov 2026 10:00:00 +0100", "Subject: Оренда за листопад"):
        assert line in text.splitlines()
    assert "Rent for the Lisbon flat is 950 € per month." in text


def test_multipart_mixed_with_two_text_parts_keeps_both(tmp_path):
    message = _message()
    message.set_content("First part: rent due on the 5th.")
    message.add_attachment("Second part: the deposit is unchanged.", subtype="plain")
    message.get_payload()[1].replace_header("Content-Disposition", "inline")
    text = _read(tmp_path, message).text
    assert "First part: rent due on the 5th." in text and "Second part: the deposit is unchanged." in text


def test_html_only_goes_through_the_page_reader_and_a_plain_alternative_wins(tmp_path):
    html = ('<h2>Invoice</h2><p>Pay <a href="https://example.org/rent?id=7&utm_source=mail">online</a>.</p>'
            "<script>x()</script>")
    only = _message()
    only.set_content(html, subtype="html")
    text = _read(tmp_path, only).text
    assert "Invoice" in text and "x()" not in text
    assert "utm_source" not in text

    both = _message()
    both.set_content("PLAIN VERSION")
    both.add_alternative("<p>HTML VERSION</p>", subtype="html")
    text = _read(tmp_path, both).text
    assert "PLAIN VERSION" in text and "HTML VERSION" not in text


def test_a_nested_message_keeps_its_own_headers_and_body(tmp_path):
    inner = email.message.EmailMessage(policy=email.policy.default)
    inner["From"] = "clara@example.com"
    inner["Subject"] = "Forwarded: key copies"
    inner.set_content("Two key copies are ready.")
    outer = _message()
    outer.set_content("See below.")
    outer.add_attachment(inner)
    result = _read(tmp_path, outer)
    assert "See below." in result.text
    assert "From: clara@example.com" in result.text and "Subject: Forwarded: key copies" in result.text
    assert "Two key copies are ready." in result.text
    assert "Attachments:" not in result.text


def test_attachments_are_listed_by_name_type_and_size_and_never_read(tmp_path):
    message = _message()
    message.set_content("Invoice attached.")
    message.add_attachment(b"%PDF-1.4 SECRET-PDF-BODY" + b"x" * 49_000, maintype="application", subtype="pdf",
                           filename="invoice FA-0137.pdf")
    message.add_attachment(b"JPEGDATA" * 150_000, maintype="image", subtype="jpeg", filename="photo.jpg")
    text = _read(tmp_path, message).text
    assert text.splitlines()[-1] == ("Attachments: invoice FA-0137.pdf (application/pdf, 48 KB), "
                                     "photo.jpg (image/jpeg, 1.1 MB)")
    assert "SECRET-PDF-BODY" not in text and "JPEGDATA" not in text


def test_a_cp1251_quoted_printable_body_is_decoded(tmp_path):
    message = _message()
    message.set_content("Договір оренди укладено на один рік.", charset="cp1251", cte="quoted-printable")
    assert "Договір оренди укладено на один рік." in _read(tmp_path, message).text


def test_the_part_cap_stops_the_walk_and_marks_truncation(tmp_path):
    message = _message()
    message.set_content("part 0")
    for n in range(1, 260):
        message.add_attachment(f"part {n}", subtype="plain")
        message.get_payload()[-1].replace_header("Content-Disposition", "inline")
    result = _read(tmp_path, message)
    assert "part 0" in result.text and "part 150" in result.text and "part 258" not in result.text
    assert result.truncated is True


def test_the_depth_cap_stops_a_deep_nest(tmp_path):
    body = email.message.EmailMessage(policy=email.policy.default)
    body["Subject"] = "level 0"
    body.set_content("text at level 0")
    for level in range(1, readers._EML_MAX_DEPTH + 4):
        wrapper = email.message.EmailMessage(policy=email.policy.default)
        wrapper["Subject"] = f"level {level}"
        wrapper.set_content(f"text at level {level}")
        wrapper.add_attachment(body)
        body = wrapper
    result = _read(tmp_path, body)
    assert f"text at level {readers._EML_MAX_DEPTH + 3}" in result.text      # the outermost message is read
    assert "text at level 0" not in result.text
    assert result.truncated is True

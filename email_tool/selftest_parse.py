"""
Stdlib-only self-test for email_parse: forwarded-thread handling + attachment
METADATA extraction.

Attachment content is deliberately never forwarded to Clark — documents enter
Clark through the Claude CIM-intake skill. The poller only sends attachment
metadata (filename/content_type/size_bytes) so Clark's reply can tell the
sender their PDF was not processed.

No pytest / no third-party deps (email_parse imports only the stdlib):

    python3 email_tool/selftest_parse.py

Exits non-zero on the first failed assertion.
"""

from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import email_parse

FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def _build_forwarded_with_pdf() -> bytes:
    """A forwarded thread (banker signature buried) + a PDF CIM attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = "Bret Forster <bforster@willcrest.com>"
    msg["To"] = "clark@willcrestpartners.com"
    msg["Subject"] = "Fwd: Project Falcon CIM"
    msg["Message-ID"] = "<forward-1@willcrest.com>"

    body = (
        "Clark, please add Rich as a contact.\n\n"
        "---------- Forwarded message ----------\n"
        "From: Rich Banker <rich@peakadvisors.com>\n"
        "Subject: Project Falcon\n\n"
        "Attached is the teaser for your review.\n\n"
        "Rich Banker\n"
        "Managing Director | Peak Advisors\n"
        "Mobile: +1 (312) 555-9090\n"
        "rich@peakadvisors.com\n"
    )
    msg.attach(MIMEText(body, "plain"))

    pdf = MIMEApplication(FAKE_PDF, _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="Project Falcon CIM.pdf")
    msg.attach(pdf)
    return msg.as_bytes()


def check(name, cond, detail=""):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  FAIL {name}" + (f"\n      {detail}" if detail else ""))
        raise SystemExit(1)


def main():
    raw = _build_forwarded_with_pdf()

    parsed = email_parse.parse_message(raw)
    check("from email parsed", parsed["from"]["email"] == "bforster@willcrest.com", parsed["from"])
    check("subject parsed", parsed["subject"] == "Fwd: Project Falcon CIM")
    check(
        "latest message strips the forwarded history",
        "Forwarded message" not in parsed["instruction_text"]
        and "please add rich" in parsed["instruction_text"].lower(),
        parsed["instruction_text"],
    )
    check(
        "full thread retains the buried banker signature",
        "Peak Advisors" in parsed["body_full_text"] and "555-9090" in parsed["body_full_text"],
        parsed["body_full_text"],
    )

    atts = email_parse.extract_attachments(raw)
    check("one attachment found", len(atts) == 1, str(len(atts)))
    a = atts[0]
    check("attachment filename decoded", a["filename"] == "Project Falcon CIM.pdf", a["filename"])
    check("attachment content-type is pdf", "pdf" in a["content_type"], a["content_type"])
    check("attachment size reported", a["size"] == len(FAKE_PDF), str(a["size"]))

    # A plain text-only message yields no attachments (envelope stays unchanged).
    plain = MIMEText("Just an FYI, no action needed.", "plain")
    plain["From"] = "bforster@willcrest.com"
    plain["Subject"] = "fyi"
    check("text-only mail has no attachments", email_parse.extract_attachments(plain.as_bytes()) == [])

    print("\nAll parse self-tests passed.")


if __name__ == "__main__":
    main()

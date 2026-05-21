import re

from django.http import HttpResponse
from django.template.loader import render_to_string


def _escape_pdf_text(value):
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fallback_pdf_from_html(html_string):
    text = re.sub(r"<br\s*/?>", "\n", html_string, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|h1|h2|h3)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = lines[:55]

    y = 800
    content_lines = ["BT", "/F1 10 Tf"]
    for line in lines:
        content_lines.append(f"1 0 0 1 50 {y} Tm ({_escape_pdf_text(line[:95])}) Tj")
        y -= 14
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode("ascii")
    )
    return bytes(pdf)


def _xhtml2pdf_from_html(html_string):
    import io
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    result = pisa.CreatePDF(html_string, dest=buf)
    if result.err:
        return None
    return buf.getvalue()


def generate_pdf(template_name, context):
    html_string = render_to_string(template_name, context)

    # Try WeasyPrint first (best quality, needs GTK on Windows)
    try:
        from weasyprint import HTML
        pdf_file = HTML(string=html_string).write_pdf()
        response = HttpResponse(pdf_file, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="document.pdf"'
        return response
    except (ImportError, OSError, Exception):
        pass

    # Try xhtml2pdf (pure Python, works on Windows without native libs)
    try:
        pdf_file = _xhtml2pdf_from_html(html_string)
        if pdf_file:
            response = HttpResponse(pdf_file, content_type="application/pdf")
            response["Content-Disposition"] = 'inline; filename="document.pdf"'
            return response
    except Exception:
        pass

    # Last resort: plain-text PDF
    pdf_file = _fallback_pdf_from_html(html_string)
    response = HttpResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="document.pdf"'
    return response
